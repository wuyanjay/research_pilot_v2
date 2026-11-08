"""临时指定论文与假模型验证 C 类内容流、引用、补读及实际 HTTP 交付。"""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agenthack.telemetry import Telemetry
from fastapi.testclient import TestClient
import httpx
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import pymupdf

from src.agent import ResearchPilotAgent
from src.handlers.survey import (
    SurveyHandler, assemble_markdown, MAX_BATCH_CHARS, REPAIR_FEEDBACK_CHARS,
    ModelStructureError, StageFailure, _check_outline, _check_overview, _check_notes,
)
from src.handlers.survey_prompts import OVERVIEW_PROMPT, build_messages
from src.llm.client import LLMError, LLMTruncatedError
from src.runtime import ResearchPilotRuntime


def question(names):
    return {
        "case_id": "C-01", "function": "Survey", "title": "模拟处理路线与环境影响综述",
        "input": "比较指定论文的处理条件、资源消耗与环境影响，至少800字符，引用至少5篇指定论文。",
        "materials": names, "constraints": ["只依据指定材料"],
        "expected_output": "返回完整 Markdown 综述及保存路径", "output_schema_ref": "Survey",
    }


def outline():
    return {
        "scope": "比较处理路线的条件与环境影响",
        "sections": [
            {"title": "方法与条件", "questions": ["如何处理、评价边界是什么？"],
             "information_needs": ["方法、资源消耗及计算口径"], "keywords": {"zh": ["能耗"], "en": ["energy"]}},
            {"title": "影响与局限", "questions": ["主要结果和适用范围是什么？"],
             "information_needs": ["结果、条件与局限"], "keywords": {"zh": ["环境影响"], "en": ["impacts"]}},
        ],
        "requirements": {"min_chars": 800, "min_references": 5},
    }


def overview(ids):
    return {
        "papers": [
            {
                "paper_id": number,
                "research_focus": "模拟路线与资源消耗",
                "known_information": [
                    {"question_id": "Q2", "information": "概括性原文说明研究资源消耗与环境影响"},
                ],
                "reading_questions": [
                    {"question_id": "Q1", "keywords": ["energy", "impacts"]},
                ],
            }
            for number in ids
        ]
    }


def notes(ids, extra=False):
    return {"papers": [{
        "paper_id": number, "research_focus": "模拟路线与评价条件",
        "methods": ["比较模拟路线"], "conditions": ["按单位材料和明确边界评价"],
        "findings": [{"statement": "补读增加了评价边界说明" if extra else "在给定条件下报告资源消耗结果",
                      "kind": "reported_result", "section_ids": [1, 2], "numbers": "无额外数值"}],
        "limitations": ["不能推广到其他条件"], "gaps": [],
    } for number in ids]}


def comparison(tasks=None):
    return {"sections": [{"title": "跨论文方法与条件比较", "points": [{"analysis": "比较对象和评价边界后解释差异", "paper_ids": [1, 2, 3, 4, 5]}], "gaps": []}],
            "supplement_tasks": tasks or []}


def body(markers=None):
    markers = markers or [f"[{number}]" for number in range(1, 6)]
    paragraph = "这些内容是本地测试资料中的模拟结论，比较时需要保留研究对象、方法、资源口径和评价边界，避免把不同条件下的结果直接排名。"
    # 测试正文长度与引用数量独立，确保单组复合引用也能进入引用检查。
    repetitions = max(4, 16 // len(markers) + 1)
    return "# 模拟处理路线综述\n\n## 方法、结果与适用条件\n\n" + "\n\n".join(paragraph * repetitions + marker for marker in markers)


def write_pdf(path):
    texts = [
        "Test paper\nPublished: 2024\nAbstract: Study energy and impacts of treatment routes.\nKeywords: energy; impacts\n1. Introduction",
        "Methods\nEnergy measured per kg within the stated study boundary.",
        "Results\nThe treatment reduced energy and impacts under stated conditions.",
        "Discussion\nThe conclusions apply only to this setting.",
        "Appendix\nAdditional boundary evidence for the focused follow-up question.",
    ]
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page()
            page.insert_text((40, 80), text, fontsize=12)
        document.save(path)


class FakeLLM:
    timeout_s = 900

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, **options):
        self.calls.append((deepcopy(messages), options))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


class PageProbe:
    """仅提供已有页级命中，观察 Handler 的选页与读取范围。"""

    def __init__(self, hits, total_pages=20):
        self.hits = hits
        self.total_pages = total_pages
        self.searches = []
        self.reads = []

    def search_pdf_text(self, paths, keywords, limit):
        self.searches.append((list(paths), list(keywords), limit))
        return deepcopy(self.hits)

    def read_pdf_pages(self, path, pages=None):
        if pages is None:
            raise AssertionError("无命中时不应自动读取整篇论文")
        self.reads.append(list(pages))
        return {
            "filename": Path(path).name,
            "path": str(path),
            "total_pages": self.total_pages,
            "pages": [
                {"page": page, "text": f"Page {page}: actual methods, energy and impacts.", "method": "text"}
                for page in pages
            ],
        }


class SurveyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.papers = self.root / "papers"
        self.papers.mkdir()
        self.names = [f"指定论文 {number}.pdf" for number in range(1, 6)]
        for name in self.names:
            write_pdf(self.papers / name)
        self.question = question(self.names.copy())
        provider = TracerProvider()
        self.addCleanup(provider.shutdown)
        self.exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        session = Telemetry(provider=provider).session(task_id="survey-offline", task_type="Survey")
        session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)

    def handler(self, replies):
        return SurveyHandler(FakeLLM(replies), papers_directory=self.papers, work_directory=self.root / "work", team="测试队")

    def sources(self):
        return [{"paper_id": number, "filename": name, "read_pages": [1, 2, 3, 4, 5]} for number, name in enumerate(self.names, 1)]

    async def test_outline_drives_reading_and_writing_preserves_sources_and_materials(self):
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), body()])
        original = deepcopy(self.question)
        result = await handler.handle(self.question)
        self.assertEqual(set(result), {"case_id", "path", "markdown"})
        self.assertEqual(result["path"], "data/expert/C-01-综述-测试队.md")
        self.assertEqual(self.question, original)
        self.assertEqual(len(handler.llm.calls), 5)
        self.assertIn("## 参考文献", result["markdown"])
        for number, name in enumerate(self.names, 1):
            self.assertIn(f"[{number}] {name}", result["markdown"])
        for messages, options in handler.llm.calls:
            self.assertIn(self.question["input"], messages[1]["content"])
            self.assertEqual(options["max_retries"], 0)
            self.assertTrue(0 < options["timeout_s"] <= 180)
            self.assertNotIn("max_tokens", options)
        overview_input = self.input(handler, 1)
        self.assertEqual(overview_input["outline"]["sections"][0]["section_id"], 1)
        self.assertEqual([item["paper_id"] for item in overview_input["papers"]], list(range(1, 6)))
        writing_input = self.input(handler, 4)
        self.assertEqual(len(writing_input["notes"]), 5)
        self.assertEqual({item["paper_id"] for item in writing_input["pages"]}, set(range(1, 6)))
        self.assertEqual(len(list((self.root / "work/pdf_cache").glob("*.json"))), 5)

    def input(self, handler, index):
        return json.loads(handler.llm.calls[index][0][1]["content"].split("输入：\n", 1)[1])

    def test_outline_assigns_stable_question_ids_without_modifying_model_reply(self):
        reply = outline()
        reply["sections"][0]["question_id"] = "模型自行生成的编号"
        original = deepcopy(reply)
        checked = _check_outline(reply)
        questions = checked["reading_questions"]
        self.assertEqual([item["question_id"] for item in questions], ["Q1", "Q2"])
        self.assertEqual([item["section_id"] for item in questions], [1, 2])
        self.assertEqual(questions[0]["question"], reply["sections"][0]["questions"][0])
        self.assertIn("energy", questions[0]["keywords"])
        self.assertEqual(checked["sections"][0]["question_id"], "Q1")
        self.assertEqual(reply, original)

    def test_overview_merges_same_question_id_and_uses_program_question_text(self):
        frame = _check_outline(outline())
        response = overview([1])
        response["papers"][0]["reading_questions"] = [
            {"question_id": "Q1", "question": "模型换了一种问法", "keywords": ["energy"]},
            {"question_id": "Q1", "question": "另一种问法", "keywords": ["Energy", "impacts"]},
        ]
        records = _check_overview(response, [{"paper_id": 1}], frame)
        tasks = records[0]["reading_questions"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["question_id"], "Q1")
        self.assertEqual(tasks[0]["question"], frame["reading_questions"][0]["question"])
        terms = [word.casefold() for word in tasks[0]["keywords"]]
        self.assertIn("energy", terms)
        self.assertIn("impacts", terms)
        self.assertEqual(len(terms), len(set(terms)))
        self.assertEqual(records[0]["known_information"][0]["question_id"], "Q2")

    def test_overview_rejects_unknown_question_id(self):
        frame = _check_outline(outline())
        response = overview([1])
        response["papers"][0]["reading_questions"][0]["question_id"] = "Q999"
        with self.assertRaises(ModelStructureError) as raised:
            _check_overview(response, [{"paper_id": 1}], frame)
        self.assertEqual(raised.exception.partial_records[0]["reading_questions"], [])

    def test_overview_keeps_unique_valid_records_and_does_not_guess_unknown_or_duplicate_ids(self):
        frame = _check_outline(outline())
        response = overview([1, 1, 2, 99])
        response["papers"][1]["research_focus"] = "重复编号不能按位置猜论文"
        with self.assertRaises(ModelStructureError) as raised:
            _check_overview(response, [{"paper_id": n} for n in range(1, 6)], frame)
        records = raised.exception.partial_records
        self.assertEqual([record["paper_id"] for record in records], [2])

    def test_notes_preserve_valid_findings_and_do_not_relabel_invalid_kind(self):
        frame = _check_outline(outline())
        response = notes([1, 2])
        response["papers"][0]["findings"].append({
            "statement": "有效结果保留", "kind": "reported_result", "section_ids": [13],
        })
        response["papers"][0]["findings"].append({
            "statement": "类型错误不得升级为结果", "kind": "unknown_kind", "section_ids": [1],
        })
        response["papers"][1]["research_focus"] = ""
        with self.assertLogs("src.handlers.survey", level="WARNING") as captured:
            with self.assertRaises(ModelStructureError) as raised:
                _check_notes(response, [{"paper_id": n} for n in range(1, 6)], frame)
        records = raised.exception.partial_records
        self.assertEqual([record["paper_id"] for record in records], [1, 2])
        findings = records[0]["findings"]
        self.assertEqual([item["statement"] for item in findings], ["在给定条件下报告资源消耗结果", "有效结果保留"])
        self.assertEqual(findings[-1]["kind"], "reported_result")
        self.assertEqual(findings[-1]["section_ids"], [])
        diagnostic = "\n".join(captured.output)
        self.assertIn("field_paths", diagnostic)
        self.assertNotIn("有效结果保留", diagnostic)

    async def test_known_summary_information_does_not_drop_designated_papers(self):
        initial = overview(range(1, 6))
        for paper in initial["papers"]:
            paper["known_information"].append({
                "question_id": "Q1", "information": "概括性原文已说明处理路线与资源评价边界",
            })
            paper["reading_questions"] = []
        handler = self.handler([outline(), initial, notes(range(1, 6)), comparison(), body()])
        result = await handler.handle(self.question)
        self.assertEqual(result["case_id"], self.question["case_id"])
        self.assertEqual({paper["paper_id"] for paper in self.input(handler, 2)["papers"]}, set(range(1, 6)))
        self.assertEqual(len(self.input(handler, 3)["notes"]), 5)

    async def test_fenced_overview_json_does_not_consume_retry(self):
        reply = "```json\n" + json.dumps(overview(range(1, 6)), ensure_ascii=False) + "\n```"
        handler = self.handler([outline(), reply, notes(range(1, 6)), comparison(), body()])
        await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 5)

    async def test_invalid_json_repair_includes_bad_reply_but_not_in_diagnostic_logs(self):
        invalid = "模型原始错误回复不应写入诊断日志::不是JSON"
        handler = self.handler([outline(), invalid, overview(range(1, 6)), notes(range(1, 6)), comparison(), body()])
        with self.assertLogs("src.handlers.survey", level="INFO") as captured:
            await handler.handle(self.question)
        repair_messages = handler.llm.calls[2][0]
        self.assertEqual(repair_messages[:2], handler.llm.calls[1][0][:2])
        self.assertTrue(any(message["role"] == "assistant" and invalid in message["content"] for message in repair_messages))
        self.assertNotIn(invalid, "\n".join(captured.output))
        self.assertEqual(len(handler.llm.calls), 6)

    async def test_diagnostics_include_request_measurements_without_question_or_reply(self):
        self.question["input"] += " 诊断日志禁止输出这段原题::secret-question"
        reply = body() + "\n\n诊断日志禁止输出这段模型回复::secret-reply"
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), reply])
        with self.assertLogs("src.handlers.survey", level="INFO") as captured:
            await handler.handle(self.question)
        output = "\n".join(captured.output)
        self.assertIn("材料整理", output)
        self.assertRegex(output, "timeout|等待|超时")
        self.assertRegex(output, "elapsed|耗时")
        self.assertNotIn("secret-question", output)
        self.assertNotIn("secret-reply", output)
        self.assertNotIn(self.question["input"], output)

    async def test_one_focused_supplement_updates_notes_without_repeating_old_pages(self):
        tasks = [{"paper_id": 1, "question": "附录有无补充边界信息？", "keywords": ["Additional"]}]
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(tasks),
                                notes([1], extra=True), comparison(tasks), body()])
        await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 7)
        extra_input = self.input(handler, 4)
        self.assertEqual([item["paper_id"] for item in extra_input["papers"]], [1])
        self.assertEqual([page["page"] for page in extra_input["papers"][0]["pages"]], [5])
        self.assertFalse(self.input(handler, 5)["supplement_allowed"])
        final_notes = self.input(handler, 6)["notes"]
        self.assertEqual(len(final_notes[0]["findings"]), 2)
        initial_pages = {
            page["page"]
            for page in self.input(handler, 2)["papers"][0]["pages"]
        }
        self.assertNotIn(5, initial_pages)
        self.assertEqual(set(final_notes[0]["source_pages"]), initial_pages | {5})

    async def test_budget_deferred_supplement_keeps_unresolved_tasks_for_writing(self):
        tasks = [{"paper_id": 1, "question": "附录是否补充评价边界？", "keywords": ["Additional"]}]
        current_time = [0.0]

        class AdvancingLLM(FakeLLM):
            async def complete(self, messages, **options):
                reply = await super().complete(messages, **options)
                if len(self.calls) == 4:
                    current_time[0] = 650.0
                return reply

        handler = self.handler([])
        handler.llm = AdvancingLLM([
            outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(tasks), body(),
        ])
        clock = SimpleNamespace(monotonic=lambda: current_time[0])
        with patch("src.handlers.survey.time", clock):
            await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 5)
        self.assertEqual(self.input(handler, 4)["unresolved_questions"], tasks)

    async def test_supplement_without_new_pages_keeps_tasks_and_skips_repeated_comparison(self):
        # measured 只出现在第2页；初轮已整理第1、2、3页，故其相邻页也无新增证据。
        tasks = [{"paper_id": 1, "question": "测量口径是否仍有未解释的限制？", "keywords": ["measured"]}]
        handler = self.handler([
            outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(tasks), body(),
        ])
        await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 5)
        initial_pages = {page["page"] for page in self.input(handler, 2)["papers"][0]["pages"]}
        self.assertTrue({1, 2, 3}.issubset(initial_pages))
        self.assertEqual(self.input(handler, 4)["unresolved_questions"], tasks)

    async def test_unreadable_summary_fails_before_overview_instead_of_dropping_paper(self):
        handler = self.handler([outline()])
        unreadable = {
            "total_pages": 2,
            "abstract_status": "not_found",
            "front_matter": [],
            "pages": [{"page": 1, "text": " \n ", "method": "ocr"}],
        }
        with patch("src.handlers.survey.read_abstract", return_value=unreadable):
            with self.assertRaisesRegex(ValueError, "概括原文"):
                await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 1)

    async def test_batches_cover_all_papers_and_are_not_combined_as_rankings(self):
        replies = [outline(), overview([1, 2]), overview([3, 4]), overview([5]),
                   notes([1, 2]), notes([3, 4]), notes([5]), comparison(), body()]
        handler = self.handler(replies)
        with patch("src.handlers.survey.MAX_BATCH_PAPERS", 2):
            result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 9)
        self.assertEqual(len(self.input(handler, 7)["notes"]), 5)
        self.assertIn(self.names[-1], result["markdown"])

    async def test_overview_batches_share_one_retry(self):
        handler = self.handler([
            outline(), overview([1, 2, 3]), {"papers": []}, {"papers": []},
            notes([1, 2, 3]), notes([4, 5]), comparison(), body(),
        ])
        with patch("src.handlers.survey.MAX_BATCH_PAPERS", 3):
            result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 8)
        # 后一批失败只退化为程序提纲中的阅读任务，不能抹掉前一批成功结果。
        overview_inputs = [
            payload["papers"]
            for payload in (self.input(handler, 1), self.input(handler, 2), self.input(handler, 3))
            if "papers" in payload
        ]
        self.assertEqual([[item["paper_id"] for item in batch] for batch in overview_inputs], [[1, 2, 3], [4, 5], [4, 5]])
        writing = self.input(handler, 7)
        self.assertEqual({item["paper_id"] for item in writing["notes"]}, set(range(1, 6)))

    async def test_partial_overview_repair_merges_valid_records_from_both_attempts(self):
        initial = overview([1, 2, 3, 4, 5])
        initial["papers"][0]["known_information"] = [
            {"question_id": "Q2", "information": "第一轮有效信息A"},
        ]
        initial["papers"][1]["known_information"] = [
            {"question_id": "Q999", "information": "无效字段触发修复"},
        ]
        repair = overview([2])
        repair["papers"][0]["known_information"] = [
            {"question_id": "Q1", "information": "修复轮有效信息B"},
        ]
        handler = self.handler([outline(), initial, repair, notes(range(1, 6)), comparison(), body()])
        await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)
        # 原轮通过的论文1及修复轮返回的论文2信息均到达材料整理。
        gathered = self.input(handler, 3)["papers"]
        by_id = {paper["paper_id"]: paper for paper in gathered}
        self.assertIn("第一轮有效信息A", json.dumps(by_id[1], ensure_ascii=False))
        self.assertIn("修复轮有效信息B", json.dumps(by_id[2], ensure_ascii=False))

    async def test_supplement_does_not_reset_notes_retry(self):
        task = [{"paper_id": 1, "question": "补读附录", "keywords": ["Additional"]}]
        handler = self.handler([outline(), overview(range(1, 6)), {"papers": []}, notes(range(1, 6)),
                                comparison(task), {"papers": []}, body()])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 7)
        # 补读整理失败后，初次有效比较仍用于写作。
        self.assertEqual(self.input(handler, 6)["outline"][0]["title"], "跨论文方法与条件比较")

    async def test_partial_notes_survive_exhaustion_while_other_papers_use_source_pages(self):
        partial = notes([1])
        handler = self.handler([
            outline(), overview(range(1, 6)), partial, {"papers": []}, comparison(), body(),
        ])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 6)
        writing = self.input(handler, 5)
        by_id = {record["paper_id"]: record for record in writing["notes"]}
        self.assertIn("在给定条件下报告资源消耗结果", json.dumps(by_id[1], ensure_ascii=False))
        self.assertTrue(all(not record["findings"] for number, record in by_id.items() if number != 1))
        self.assertTrue({1, 2, 3, 4, 5}.issubset({page["paper_id"] for page in writing["pages"]}))

    async def test_exhausted_notes_stage_still_writes_with_actual_read_pages(self):
        handler = self.handler([
            outline(), overview(range(1, 6)), {"papers": []}, {"papers": []}, comparison(), body(),
        ])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 6)
        writing = self.input(handler, 5)
        self.assertEqual({page["paper_id"] for page in writing["pages"]}, set(range(1, 6)))
        self.assertTrue(all(page["text"].strip() for page in writing["pages"]))
        self.assertTrue(all(not note["findings"] for note in writing["notes"]))

    async def test_exhausted_notes_without_any_read_evidence_still_fails(self):
        handler = self.handler([outline(), overview(range(1, 6)), {"papers": []}, {"papers": []}])
        with patch.object(handler, "_read_tasks", return_value=[]):
            with self.assertRaises(StageFailure):
                await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 2)

    async def test_comparison_exhaustion_uses_outline_and_valid_notes_for_writing(self):
        handler = self.handler([
            outline(), overview(range(1, 6)), notes(range(1, 6)), {"sections": []}, {"sections": []}, body(),
        ])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 6)
        writing = self.input(handler, 5)
        self.assertEqual(len(writing["notes"]), 5)
        self.assertEqual([section["title"] for section in writing["outline"]],
                         [section["title"] for section in outline()["sections"]])
        self.assertTrue(writing["pages"])

    async def test_post_supplement_comparison_failure_keeps_previous_comparison(self):
        task = [{"paper_id": 1, "question": "附录是否补充评价边界？", "keywords": ["Additional"]}]
        previous = comparison(task)
        previous["sections"][0]["title"] = "首次比较应保留"
        handler = self.handler([
            outline(), overview(range(1, 6)), notes(range(1, 6)), previous,
            notes([1], extra=True), {"sections": []}, {"sections": []}, body(),
        ])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 8)
        self.assertEqual(self.input(handler, 7)["outline"][0]["title"], "首次比较应保留")

    async def test_report_repair_feedback_addresses_length_and_preserves_original_input(self):
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), "# 太短\n[1]", body()])
        await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)
        self.assertIn("字符", handler.llm.calls[-1][0][-1]["content"])
        self.assertEqual(handler.llm.calls[-1][0][:2], handler.llm.calls[-2][0][:2])

    async def test_partial_citation_coverage_gets_one_targeted_repair_and_accepts_fifth_source(self):
        first = body(["[1]", "[2]", "[3]", "[4]"])
        repaired = body(["[1]", "[2]", "[3]", "[4]", "[5]"])
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first, repaired])
        result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)
        self.assertIn("[5]", result["markdown"])
        initial_messages = handler.llm.calls[4][0]
        repair_messages = handler.llm.calls[5][0]
        self.assertNotEqual(initial_messages[:2], repair_messages[:2])
        repair_payload = json.loads(repair_messages[1]["content"].split("输入：\n", 1)[1])
        self.assertEqual(repair_payload["candidate_markdown"], first.split("\n\n## 参考文献", 1)[0])
        self.assertEqual(repair_payload["missing_sources"][0]["paper_id"], 5)
        self.assertEqual(repair_payload["missing_sources"][0]["filename"], self.names[4])
        self.assertIn("question", repair_payload)
        self.assertLessEqual(len(json.dumps(repair_messages, ensure_ascii=False)), 64_000)

    async def test_partial_coverage_keeps_first_draft_if_repair_does_not_improve_or_fails(self):
        first = body(["[1]", "[2]", "[3]", "[4]"])
        cases = [
            (body(["[1]", "[2]", "[3]", "[4]"]).replace("这些内容", "修订后的不同正文"), "same-count"),
            (body(["[1]", "[2]", "[3]"]), "worse"),
            (LLMError("请求超时", retryable=True), "retryable-error"),
            (LLMTruncatedError("截断"), "truncated"),
            ("# malformed \n" + "正文" * 2_000 + " [1,2,3,4] [99]", "unknown-source"),
            ("# 超长修复\n" + "字" * 5_100 + " [1,2,3,4,5]", "oversized"),
        ]
        for repaired, label in cases:
            with self.subTest(label=label):
                handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first, repaired])
                candidate = None
                with self.assertRaises(ValueError) as raised:
                    assemble_markdown(first, self.sources(), {"min_chars": 800, "min_references": 5})
                candidate = raised.exception.markdown
                result = await handler.handle(self.question)
                self.assertEqual(result["markdown"], candidate)
                self.assertEqual(len(handler.llm.calls), 6)

    async def test_three_to_four_citations_updates_best_candidate(self):
        first = body(["[1]", "[2]", "[3]"])
        repaired = body(["[1]", "[2]", "[3]", "[4]"])
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first, repaired])
        result = await handler.handle(self.question)
        self.assertIn(f"[4] {self.names[3]}", result["markdown"])
        self.assertNotIn("[5] 指定论文5.pdf", result["markdown"])
        self.assertEqual(len(handler.llm.calls), 6)

    async def test_zero_citations_remains_a_normal_failed_writing_attempt(self):
        uncited = "# 无引用综述\n\n" + "足够长度的无引用正文。" * 120
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), uncited, uncited])
        with self.assertRaisesRegex(ValueError, "至少需要 5"):
            await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)

    async def test_existing_partial_candidate_is_returned_without_request_when_budget_is_exhausted(self):
        current_time = [0.0]

        class ExhaustingLLM(FakeLLM):
            async def complete(self, messages, **options):
                reply = await super().complete(messages, **options)
                if len(self.calls) == 5:
                    current_time[0] = 880.0
                return reply

        handler = self.handler([])
        handler.llm = ExhaustingLLM([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(),
                                     body(["[1]", "[2]", "[3]", "[4]"])])
        with patch("src.handlers.survey.time", SimpleNamespace(monotonic=lambda: current_time[0])):
            result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 5)
        self.assertIn("[4]", result["markdown"])
        self.assertNotIn("[5]", result["markdown"])

    async def test_partial_candidate_survives_spent_write_retry_without_extra_request(self):
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(),
                                LLMError("请求超时", retryable=True), body(["[1]", "[2]", "[3]", "[4]"])])
        result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)
        self.assertIn("[4]", result["markdown"])
        self.assertNotIn("[5]", result["markdown"])

    def test_oversized_coverage_repair_is_rejected_before_any_model_request(self):
        handler = self.handler([])
        with self.assertRaises(ValueError) as raised:
            assemble_markdown(body(["[1]", "[2]", "[3]", "[4]"]), self.sources(),
                              {"min_chars": 800, "min_references": 5})
        candidate = raised.exception
        question_with_large_title = {**self.question, "title": "标题" * 40_000}
        repair = handler._coverage_repair_messages(
            question_with_large_title, self.sources(), candidate,
            {number: notes([number])["papers"][0] for number in range(1, 6)}, comparison(), outline()["requirements"],
        )
        self.assertIsNone(repair)
        self.assertEqual(handler.llm.calls, [])

    async def test_external_cancel_and_nonretryable_error_during_coverage_repair_propagate(self):
        first = body(["[1]", "[2]", "[3]", "[4]"])
        for error in (asyncio.CancelledError(), LLMError("认证错误", status_code=401)):
            with self.subTest(error=type(error).__name__):
                handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first, error])
                with self.assertRaises(type(error)) as raised:
                    await handler.handle(self.question)
                self.assertIs(raised.exception, error)
                self.assertEqual(len(handler.llm.calls), 6)

    async def test_coverage_fallback_log_contains_metadata_but_not_candidate_text(self):
        first = body(["[1]", "[2]", "[3]", "[4]"]) + "\n\n独有正文保密标记::private-draft"
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first,
                                LLMError("请求超时", retryable=True)])
        with self.assertLogs("src.handlers.survey", level="WARNING") as captured:
            result = await handler.handle(self.question)
        output = "\n".join(captured.output)
        metadata = json.loads(captured.records[-1].args[0])
        self.assertEqual(metadata["missing"], [5])
        self.assertNotIn("private-draft", output)
        self.assertIn("private-draft", result["markdown"])

    async def test_same_coverage_fallback_log_excludes_draft_text(self):
        first = body(["[1]", "[2]", "[3]", "[4]"]) + "\n\n秘密正文::same-count-private"
        revised = first.replace("这些内容", "不同但仍只有四篇")
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first, revised])
        with self.assertLogs("src.handlers.survey", level="WARNING") as captured:
            result = await handler.handle(self.question)
        output = "\n".join(captured.output)
        metadata = json.loads(captured.records[-1].args[0])
        self.assertEqual(metadata["cited"], [1, 2, 3, 4])
        self.assertNotIn("same-count-private", output)
        self.assertIn("same-count-private", result["markdown"])

    async def test_unexpected_model_program_exceptions_propagate_after_partial_candidate(self):
        first = body(["[1]", "[2]", "[3]", "[4]"])
        for error in (RuntimeError("bug"), KeyError("bug"), TypeError("bug"), ValueError("bug")):
            with self.subTest(error=type(error).__name__):
                handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), first, error])
                with self.assertRaises(type(error)):
                    await handler.handle(self.question)
                self.assertEqual(len(handler.llm.calls), 6)

    async def test_material_scope_is_explicit_and_fewer_than_five_is_not_silently_accepted(self):
        handler = self.handler([outline()])
        with self.assertRaisesRegex(ValueError, "数量不足"):
            await handler.handle(question(self.names[:3]))
        self.assertEqual(len(handler.llm.calls), 1)
        handler = self.handler([outline()])
        with self.assertRaises(FileNotFoundError):
            await handler.handle(question(self.names[:4] + ["missing.pdf"]))
        self.assertEqual(len(handler.llm.calls), 1)

    async def test_cancellation_and_nonretryable_errors_propagate(self):
        for error in (asyncio.CancelledError(), LLMError("认证错误", status_code=401)):
            handler = self.handler([error])
            with self.assertRaises(type(error)) as raised:
                await handler.handle(self.question)
            self.assertIs(raised.exception, error)
            self.assertEqual(len(handler.llm.calls), 1)

    async def test_recoverable_stage_does_not_swallow_program_or_request_exceptions(self):
        for error in (ValueError("bug"), TypeError("bug"), asyncio.CancelledError(),
                      LLMError("认证错误", status_code=401)):
            with self.subTest(error=type(error).__name__):
                handler = self.handler([outline(), overview(range(1, 6)), error])
                with self.assertRaises(type(error)) as raised:
                    await handler.handle(self.question)
                self.assertIs(raised.exception, error)
                self.assertEqual(len(handler.llm.calls), 3)

    async def test_pdf_io_timeout_does_not_become_a_successful_recovery(self):
        for failure in (TimeoutError("PDF I/O timeout"), OSError("PDF I/O failure")):
            with self.subTest(error=type(failure).__name__):
                handler = self.handler([outline()])
                with patch("src.handlers.survey.read_abstract", side_effect=failure):
                    with self.assertRaises(type(failure)) as raised:
                        await handler.handle(self.question)
                self.assertIs(raised.exception, failure)
                self.assertEqual(len(handler.llm.calls), 1)

    async def test_retryable_request_failure_shares_stage_repair_allowance(self):
        handler = self.handler([LLMError("请求超时", retryable=True), outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), body()])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 6)

    async def test_pdf_page_numbers_in_section_ids_are_cleared_without_dropping_finding(self):
        wrong = notes(range(1, 6))
        wrong["papers"][0]["findings"][0]["section_ids"] = [13, 17]
        handler = self.handler([
            outline(), overview(range(1, 6)), wrong, comparison(), body(),
        ])
        with self.assertLogs("src.handlers.survey", level="INFO") as captured:
            result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(self.input(handler, 2)["valid_section_ids"], [1, 2])
        notes_input = self.input(handler, 4)
        finding = notes_input["notes"][0]["findings"][0]
        self.assertEqual(finding["statement"], wrong["papers"][0]["findings"][0]["statement"])
        self.assertEqual(finding["kind"], "reported_result")
        self.assertEqual(finding["section_ids"], [])
        self.assertNotIn(wrong["papers"][0]["findings"][0]["statement"], "\n".join(captured.output))
        self.assertIn("case_id", "\n".join(captured.output))
        self.assertIn("field_paths", "\n".join(captured.output))
        self.assertIn("section_ids", "\n".join(captured.output))
        self.assertEqual(len(handler.llm.calls), 5)

    def reading_source(self):
        return {
            "paper_id": 1,
            "filename": self.names[0],
            "path": str(self.papers / self.names[0]),
            "total_pages": 20,
            "summary_pages": [1],
            "known_information": [],
        }

    def reading_tasks(self):
        return [
            {"paper_id": 1, "question_id": "Q1", "question": "方法和能耗是什么？", "keywords": ["energy"]},
            {"paper_id": 1, "question_id": "Q2", "question": "环境影响和局限是什么？", "keywords": ["impacts"]},
        ]

    def test_reading_searches_once_per_paper_and_limits_selected_pages(self):
        source = self.reading_source()
        hits = [
            {"filename": source["filename"], "path": source["path"], "page": page,
             "snippet": f"Actual evidence on page {page}.",
             "matched_keywords": ["energy"] if page % 2 == 0 else ["impacts"]}
            for page in range(2, 20)
        ]
        tools = PageProbe(hits)
        readings = self.handler([])._read_tasks(
            tools, [source], self.reading_tasks(), time.monotonic() + 900,
        )
        self.assertEqual(len(tools.searches), 1)
        self.assertEqual(set(tools.searches[0][1]), {"energy", "impacts"})
        self.assertEqual(len(readings), 1)
        pages = {page["page"] for page in readings[0]["pages"]}
        self.assertTrue(1 < len(pages) <= 3)
        self.assertTrue(any(page % 2 == 0 for page in pages))
        self.assertTrue(any(page % 2 != 0 for page in pages))
        self.assertNotIn(1, pages)

    def test_no_keyword_hit_keeps_bounded_source_pages_instead_of_whole_pdf(self):
        source = self.reading_source()
        tools = PageProbe([])
        readings = self.handler([])._read_tasks(
            tools, [source], self.reading_tasks(), time.monotonic() + 900,
        )
        self.assertEqual(len(readings), 1)
        pages = {page["page"] for page in readings[0]["pages"]}
        self.assertTrue(pages)
        self.assertLessEqual(len(pages), 3)
        self.assertLess(len(pages), source["total_pages"])

    async def test_many_planned_batches_never_start_a_fifteen_second_request(self):
        handler = self.handler([{"ok": True}])
        try:
            await handler._request(
                "材料整理", [{"role": "user", "content": "待整理的材料"}], lambda data: data,
                {"remaining": 1}, time.monotonic() + 700, 44,
            )
        except TimeoutError:
            self.assertEqual(handler.llm.calls, [])
        else:
            self.assertGreaterEqual(handler.llm.calls[0][1]["timeout_s"], 30)

    async def test_notes_cannot_spend_time_reserved_for_comparison_and_writing(self):
        handler = self.handler([])
        with self.assertRaises(TimeoutError):
            await handler._request(
                "材料整理", [{"role": "user", "content": "待整理的材料"}], lambda data: data,
                {"remaining": 1}, time.monotonic() + 50, 1,
            )
        self.assertEqual(handler.llm.calls, [])

    async def test_writing_timeout_never_exceeds_remaining_delivery_budget(self):
        handler = self.handler([body()])
        await handler._request(
            "综述写作", [{"role": "user", "content": "生成正文"}], lambda text: text,
            {"remaining": 1}, time.monotonic() + 50, 1, json_reply=False,
        )
        timeout = handler.llm.calls[0][1]["timeout_s"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 40)

    def test_notes_budget_defers_optional_pages_but_keeps_every_paper_and_priority_page(self):
        handler = self.handler([])
        frame = _check_outline(outline())
        readings = [
            {
                "paper_id": number,
                "filename": name,
                "page_priority": [3, 2, 1],
                "pages": [
                    {"page": page, "text": "actual evidence " * 400, "method": "text"}
                    for page in (1, 2, 3)
                ],
            }
            for number, name in enumerate(self.names, 1)
        ]
        original = deepcopy(readings)
        batches, _builder = handler._fit_note_batches(
            self.question, frame, readings, {"remaining": 1}, time.monotonic() + 350,
        )
        papers = [paper for batch in batches for paper in batch]
        self.assertEqual({paper["paper_id"] for paper in papers}, set(range(1, 6)))
        self.assertTrue(any(paper["deferred_pages"] for paper in papers))
        for paper in papers:
            selected = {page["page"] for page in paper["pages"]}
            self.assertIn(3, selected)
            self.assertTrue(selected)
            self.assertFalse(selected.intersection(paper["deferred_pages"]))
        self.assertEqual(readings, original)
        self.assertEqual(handler.llm.calls, [])

    async def test_notes_merged_paper_preserves_all_source_pages(self):
        handler = self.handler([notes([1])])
        readings = [{
            "paper_id": 1,
            "filename": self.names[0],
            "pages": [
                {"page": page, "text": "actual evidence " * 400, "method": "text"}
                for page in (2, 3)
            ],
        }]
        records = {}
        await handler._notes(
            self.question, _check_outline(outline()), readings, records,
            {"remaining": 1}, time.monotonic() + 900,
        )
        self.assertEqual(len(handler.llm.calls), 1)
        self.assertEqual([paper["paper_id"] for paper in self.input(handler, 0)["papers"]], [1])
        self.assertEqual(set(records[1]["source_pages"]), {2, 3})

    async def test_notes_impossible_budget_never_starts_model_or_drops_a_paper(self):
        handler = self.handler([])
        readings = [
            {"paper_id": number, "filename": name,
             "pages": [{"page": 1, "text": "actual evidence " * 3_000, "method": "text"}]}
            for number, name in enumerate(self.names, 1)
        ]
        original = deepcopy(readings)
        with self.assertRaises(StageFailure) as raised:
            await handler._notes(
                self.question, _check_outline(outline()), readings, {},
                {"remaining": 1}, time.monotonic() + 300,
            )
        self.assertEqual(raised.exception.reason, "budget")
        self.assertEqual(handler.llm.calls, [])
        self.assertEqual(readings, original)

    async def test_empty_supplement_notes_do_not_spend_budget_or_reset_records(self):
        handler = self.handler([])
        records = {1: {"paper_id": 1, "findings": ["已有材料"]}}
        original = deepcopy(records)
        frame = _check_outline(outline())
        batches, _builder = handler._fit_note_batches(
            self.question, frame, [], {"remaining": 0}, time.monotonic() + 1,
        )
        self.assertEqual(batches, [])
        await handler._notes(
            self.question, frame, [], records, {"remaining": 0}, time.monotonic() + 1,
        )
        self.assertEqual(records, original)
        self.assertEqual(handler.llm.calls, [])

    async def test_comparison_over_budget_is_split_by_chapter_with_all_papers_visible(self):
        handler = self.handler([comparison(), comparison()])
        frame = outline()
        for index, section in enumerate(frame["sections"], 1):
            section["section_id"] = index
        all_notes = {}
        for number in range(1, 6):
            record = notes([number])["papers"][0]
            record["findings"] = [{"statement": "x" * 8_000, "section_ids": [1]}, {"statement": "y" * 8_000, "section_ids": [2]}]
            all_notes[number] = record
        original = deepcopy(all_notes)
        result = await handler._compare(self.question, frame, self.sources(), all_notes, {"remaining": 1}, time.monotonic() + 900, False)
        self.assertEqual(len(handler.llm.calls), 2)
        self.assertEqual(len(result["sections"]), 2)
        for index in range(2):
            payload = self.input(handler, index)
            self.assertEqual({record["paper_id"] for record in payload["notes"]}, set(range(1, 6)))
            self.assertEqual(len(payload["outline"]["sections"]), 1)
        self.assertEqual(all_notes, original)

    async def test_exhausted_budget_never_starts_model_call(self):
        handler = self.handler([])
        with patch("src.handlers.survey.time.monotonic", side_effect=lambda values=iter([0]): next(values, 1000)):
            with self.assertRaises(TimeoutError):
                await handler.handle(self.question)
        self.assertEqual(handler.llm.calls, [])

    def test_large_writing_input_preserves_every_source_and_uses_selected_comparison_points(self):
        handler = self.handler([])
        all_notes = {}
        for number in range(1, 6):
            record = notes([number])["papers"][0]
            record["research_focus"] = [record["research_focus"]]
            record["findings"] = [{"statement": "x" * 20_000}]
            record["source_pages"] = [3]
            all_notes[number] = record
        frame = outline()
        messages = handler._writing_messages(self.question, frame, comparison(), self.sources(), all_notes, [])
        self.assertLess(len(json.dumps(messages, ensure_ascii=False)), 64_000)
        context = json.loads(messages[1]["content"].split("输入：\n", 1)[1])
        self.assertEqual({note["paper_id"] for note in context["notes"]}, set(range(1, 6)))
        self.assertTrue(all(note["conditions"] and note["comparison_points"] for note in context["notes"]))
        self.assertIn("material_scope", context)

    def test_long_paper_batches_merge_same_paper_without_losing_text_or_page_offsets(self):
        handler = self.handler([])
        text = "energy and impacts\n" * 2_000 + "END_OF_PAPER"
        units = handler._units({"paper_id": 1, "filename": self.names[0], "pages": [{"page": 3, "text": text}]})
        self.assertGreater(len(units), 1)
        self.assertTrue(units[-1]["pages"][-1]["text"].endswith("END_OF_PAPER"))
        covered = set()
        for unit in units:
            for page in unit["pages"]:
                start = page["start_char"]
                self.assertEqual(page["text"], text[start:start + len(page["text"])])
                covered.update(range(start, start + len(page["text"])))
        self.assertEqual(len(covered), len(text))
        builder = lambda batch: build_messages(OVERVIEW_PROMPT, self.question, outline=outline(), papers=batch)
        batches = handler._batches(units, builder)
        self.assertLess(len(batches), len(units))
        actual_parts = []
        expected_parts = [
            (page["page"], page["start_char"], page["text"])
            for unit in units
            for page in unit["pages"]
        ]
        for batch in batches:
            self.assertEqual([paper["paper_id"] for paper in batch], [1])
            self.assertLessEqual(
                len(json.dumps(builder(batch), ensure_ascii=False)) + REPAIR_FEEDBACK_CHARS,
                MAX_BATCH_CHARS,
            )
            actual_parts.extend(
                (page["page"], page["start_char"], page["text"])
                for paper in batch
                for page in paper["pages"]
            )
        self.assertEqual(actual_parts, expected_parts)

    def test_citations_reference_names_and_optional_pages_are_checked(self):
        requirements = {"min_chars": 800, "min_references": 5}
        markdown = assemble_markdown(body(["[1,2,3,4,5]"]), self.sources(), requirements)
        self.assertEqual(markdown.count("## 参考文献"), 1)
        self.assertIn("[5] " + self.names[4], markdown)
        old = body() + "\n\n## 参考文献\n\n[1] 模型猜测的文件名.pdf"
        self.assertNotIn("模型猜测", assemble_markdown(old, self.sources(), requirements))
        self.assertIn("[1, p. 3]", assemble_markdown(body(["[1, p. 3]", "[2]", "[3]", "[4]", "[5]"]), self.sources(), requirements))
        for bad, expected in ((body() + "[6]", "不在指定"), (body() + "[1, p. 99]", "未实际读取"),
                              (body() + "\n\n" + "\n\n".join("[1]" for _ in range(40)), "最多 40"), ("# 短文\n[1]", "字符"),
                              (body() + "x" * 5_000, "字符"), ("```markdown\n" + body() + "\n```", "直接输出")):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                assemble_markdown(bad, self.sources(), requirements)

    def test_code_links_and_reference_list_cannot_fake_body_citation_coverage(self):
        value = body(["[1]"])
        value += "\n```text\n[2][3]\n```\n`[4]`\n[5](https://example.invalid)\n"
        value += "\n## 参考文献\n[2] file.pdf\n[3] file.pdf\n[4] file.pdf\n[5] file.pdf"
        with self.assertRaisesRegex(ValueError, "至少需要 5"):
            assemble_markdown(value, self.sources(), {"min_chars": 800, "min_references": 5})

    def test_reference_heading_inside_code_does_not_cut_off_real_body(self):
        text = "# 测试综述\n\n```markdown\n## 参考文献\n```\n\n" + body()
        actual = assemble_markdown(text, self.sources(), {"min_chars": 800, "min_references": 5})
        self.assertIn("[5] " + self.names[4], actual)
        self.assertIn("## 方法、结果与适用条件", actual)

    def test_body_limit_counts_the_separator_before_references(self):
        prefix = "# 边界测试\n\n[1,2,3,4,5]\n"
        exact = prefix + "字" * (4_998 - len(prefix))
        requirements = {"min_chars": 800, "min_references": 5}
        markdown = assemble_markdown(exact, self.sources(), requirements)
        self.assertEqual(len(markdown.split("## 参考文献", 1)[0]), 5_000)
        with self.assertRaisesRegex(ValueError, "5001"):
            assemble_markdown(exact + "字", self.sources(), requirements)


class SurveyHTTPTests(unittest.TestCase):
    def test_actual_runtime_dispatch_pdf_llm_trace_and_both_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            papers = root / "input/papers"
            papers.mkdir(parents=True)
            names = [f"指定论文 {number}.pdf" for number in range(1, 6)]
            for name in names:
                write_pdf(papers / name)
            write_pdf(papers / "未授权.pdf")
            payload = question(names)
            directory = root / "output/data/测试队"
            (directory / "assigned").mkdir(parents=True)
            raw = json.dumps(dict(reversed(list(payload.items()))), ensure_ascii=False, indent=4).encode()
            (directory / "assigned/C-01.json").write_bytes(raw)
            entries = []
            for prefix, count in (("A", 1), ("B", 3), ("C", 2), ("D", 3), ("E", 2)):
                for number in range(1, count + 1):
                    case_id = f"{prefix}-{number:02d}"
                    entries.append({"case_id": case_id, "function": "Survey" if case_id == "C-01" else "unused",
                                    "title": payload["title"] if case_id == "C-01" else "unused",
                                    "question_file": f"data/测试队/assigned/{case_id}.json",
                                    "question_sha256": hashlib.sha256(raw).hexdigest() if case_id == "C-01" else "not-read"})
            dispatch = json.dumps({"team": "测试队", "case_ids": [item["case_id"] for item in entries], "cases": entries}, ensure_ascii=False).encode()
            (directory / "dispatch.json").write_bytes(dispatch)
            wrapped_overview = "```json\n" + json.dumps(overview(range(1, 6)), ensure_ascii=False) + "\n```"
            replies = iter([outline(), wrapped_overview, notes(range(1, 6)), comparison(), body()])
            requests = []

            def respond(request):
                requests.append(json.loads(request.content))
                reply = next(replies)
                return httpx.Response(200, json={
                    "id": "offline-survey", "object": "chat.completion", "created": 0, "model": "offline",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                })

            def create_client(**options):
                return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **options)

            exporter = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            self.addCleanup(provider.shutdown)
            opened = []
            original_open = pymupdf.open

            def open_allowed(path, *args, **kwargs):
                opened.append(Path(path).name)
                if Path(path).name == "未授权.pdf":
                    raise AssertionError("不能读取清单外论文")
                return original_open(path, *args, **kwargs)

            with patch.dict(os.environ, {"DATA_PATH": str(root / "input"), "RESEARCH_RESULT_ROOT": str(root / "output"), "RESEARCH_WORKSPACE_TEAM": "测试队",
                                         "CHALLENGE_LLM_BASE_URL": "https://offline.invalid/v1", "CHALLENGE_LLM_API_KEY": "offline-key", "CHALLENGE_LLM_MODEL": "offline"}):
                agent = ResearchPilotAgent()
                runtime = ResearchPilotRuntime(agent, telemetry=Telemetry(provider=provider))
                with patch("src.llm.client.AsyncOpenAI", side_effect=create_client), patch("src.tools.pdf.pymupdf.open", side_effect=open_allowed):
                    with TestClient(runtime.app, raise_server_exceptions=False) as client:
                        response = client.post("/solve", json=payload, headers={"X-Research-Task-ID": "c-offline-http"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(set(result), {"case_id", "path", "markdown"})
            self.assertEqual(result["case_id"], "C-01")
            self.assertEqual(result["path"], "data/expert/C-01-综述-测试队.md")
            self.assertEqual(json.loads((directory / "C-01.json").read_bytes()), result)
            self.assertEqual((root / "output" / result["path"]).read_text(encoding="utf-8"), result["markdown"])
            self.assertEqual((directory / "assigned/C-01.json").read_bytes(), raw)
            self.assertEqual((directory / "dispatch.json").read_bytes(), dispatch)
            self.assertEqual(set(opened), set(names))
            self.assertEqual(len(requests), 5)
            spans = exporter.get_finished_spans()
            model_spans = [span for span in spans if span.name == "agent.llm"]
            self.assertEqual(len(model_spans), 5)
            tool_spans = [span for span in spans if span.name == "agent.tool"]
            self.assertTrue({"list_papers", "read_pdf_pages", "search_pdf_text", "write_report_markdown", "write_result_json"}.issubset({span.attributes["tool_name"] for span in tool_spans}))
            self.assertEqual(len({span.context.trace_id for span in model_spans + tool_spans}), 1)
            self.assertFalse(list((root / "output").rglob("*.tmp")))


    def test_partial_coverage_http_delivery_keeps_four_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            papers = root / "input/papers"
            papers.mkdir(parents=True)
            names = [f"指定论文 {number}.pdf" for number in range(1, 6)]
            for name in names:
                write_pdf(papers / name)
            write_pdf(papers / "未授权.pdf")
            payload = question(names)
            directory = root / "output/data/测试队"
            (directory / "assigned").mkdir(parents=True)
            raw = json.dumps(dict(reversed(list(payload.items()))), ensure_ascii=False, indent=4).encode()
            (directory / "assigned/C-01.json").write_bytes(raw)
            entries = []
            for prefix, count in (("A", 1), ("B", 3), ("C", 2), ("D", 3), ("E", 2)):
                for number in range(1, count + 1):
                    case_id = f"{prefix}-{number:02d}"
                    entries.append({"case_id": case_id, "function": "Survey" if case_id == "C-01" else "unused",
                                    "title": payload["title"] if case_id == "C-01" else "unused",
                                    "question_file": f"data/测试队/assigned/{case_id}.json",
                                    "question_sha256": hashlib.sha256(raw).hexdigest() if case_id == "C-01" else "not-read"})
            dispatch = json.dumps({"team": "测试队", "case_ids": [item["case_id"] for item in entries], "cases": entries}, ensure_ascii=False).encode()
            (directory / "dispatch.json").write_bytes(dispatch)
            wrapped_overview = "```json\n" + json.dumps(overview(range(1, 6)), ensure_ascii=False) + "\n```"
            replies = iter([outline(), wrapped_overview, {"papers": []}, {"papers": []},
                            body(["[1]", "[2]", "[3]", "[4]"]), body(["[1]", "[2]", "[3]", "[4]"])])
            requests = []

            def respond(request):
                requests.append(json.loads(request.content))
                reply = next(replies)
                return httpx.Response(200, json={
                    "id": "offline-survey", "object": "chat.completion", "created": 0, "model": "offline",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                })

            def create_client(**options):
                return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **options)

            exporter = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            self.addCleanup(provider.shutdown)
            opened = []
            original_open = pymupdf.open

            def open_allowed(path, *args, **kwargs):
                opened.append(Path(path).name)
                if Path(path).name == "未授权.pdf":
                    raise AssertionError("不能读取清单外论文")
                return original_open(path, *args, **kwargs)

            with patch.dict(os.environ, {"DATA_PATH": str(root / "input"), "RESEARCH_RESULT_ROOT": str(root / "output"), "RESEARCH_WORKSPACE_TEAM": "测试队",
                                         "CHALLENGE_LLM_BASE_URL": "https://offline.invalid/v1", "CHALLENGE_LLM_API_KEY": "offline-key", "CHALLENGE_LLM_MODEL": "offline"}):
                agent = ResearchPilotAgent()
                runtime = ResearchPilotRuntime(agent, telemetry=Telemetry(provider=provider))
                with patch("src.llm.client.AsyncOpenAI", side_effect=create_client), patch("src.tools.pdf.pymupdf.open", side_effect=open_allowed):
                    with TestClient(runtime.app, raise_server_exceptions=False) as client:
                        response = client.post("/solve", json=payload, headers={"X-Research-Task-ID": "c-offline-http"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(set(result), {"case_id", "path", "markdown"})
            self.assertEqual(result["case_id"], "C-01")
            self.assertEqual(result["path"], "data/expert/C-01-综述-测试队.md")
            self.assertEqual(json.loads((directory / "C-01.json").read_bytes()), result)
            self.assertEqual((root / "output" / result["path"]).read_text(encoding="utf-8"), result["markdown"])
            self.assertEqual(len(result["markdown"].split("## 参考文献\n\n", 1)[1].strip().splitlines()), 4)
            self.assertEqual((directory / "assigned/C-01.json").read_bytes(), raw)
            self.assertEqual((directory / "dispatch.json").read_bytes(), dispatch)
            self.assertEqual(set(opened), set(names))
            self.assertEqual(len(requests), 6)
            spans = exporter.get_finished_spans()
            model_spans = [span for span in spans if span.name == "agent.llm"]
            self.assertEqual(len(model_spans), 6)
            tool_spans = [span for span in spans if span.name == "agent.tool"]
            self.assertTrue({"list_papers", "read_pdf_pages", "search_pdf_text", "write_report_markdown", "write_result_json"}.issubset({span.attributes["tool_name"] for span in tool_spans}))
            self.assertEqual(len({span.context.trace_id for span in model_spans + tool_spans}), 1)
            self.assertFalse(list((root / "output").rglob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
