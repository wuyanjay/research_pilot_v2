"""临时指定论文与假模型验证 C 类内容流、引用、补读及实际 HTTP 交付。"""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
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
from src.handlers.survey import SurveyHandler, assemble_markdown, MAX_BATCH_CHARS, REPAIR_FEEDBACK_CHARS
from src.handlers.survey_prompts import OVERVIEW_PROMPT, build_messages
from src.llm.client import LLMError
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
    return {"papers": [{"paper_id": number, "research_focus": "模拟路线与资源消耗",
                        "section_ids": [1, 2], "reading_questions": [{"question": "方法、结果和边界是什么？", "keywords": ["energy", "impacts"]}]} for number in ids]}


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
        self.assertEqual(set(final_notes[0]["source_pages"]), {1, 2, 3, 4, 5})

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
        handler = self.handler([outline(), {"papers": []}, overview([1, 2]), {"papers": []}])
        with patch("src.handlers.survey.MAX_BATCH_PAPERS", 2):
            with self.assertRaisesRegex(ValueError, "论文概览.*重试已用尽"):
                await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 4)

    async def test_supplement_does_not_reset_notes_retry(self):
        task = [{"paper_id": 1, "question": "补读附录", "keywords": ["Additional"]}]
        handler = self.handler([outline(), overview(range(1, 6)), {"papers": []}, notes(range(1, 6)), comparison(task), {"papers": []}])
        with self.assertRaisesRegex(ValueError, "材料整理.*重试已用尽"):
            await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)

    async def test_report_repair_feedback_addresses_length_and_preserves_original_input(self):
        handler = self.handler([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), "# 太短\n[1]", body()])
        await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)
        self.assertIn("字符", handler.llm.calls[-1][0][-1]["content"])
        self.assertEqual(handler.llm.calls[-1][0][:2], handler.llm.calls[-2][0][:2])

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

    async def test_retryable_request_failure_shares_stage_repair_allowance(self):
        handler = self.handler([LLMError("请求超时", retryable=True), outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), body()])
        result = await handler.handle(self.question)
        self.assertTrue(result["markdown"])
        self.assertEqual(len(handler.llm.calls), 6)

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
        with patch("src.handlers.survey.time.monotonic", side_effect=[0, 1000]):
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

    def test_long_paper_units_preserve_all_text_and_same_id_stays_out_of_one_batch(self):
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
        self.assertEqual(len(batches), len(units))
        for batch in batches:
            self.assertLessEqual(len(json.dumps(builder(batch), ensure_ascii=False)) + REPAIR_FEEDBACK_CHARS, MAX_BATCH_CHARS)

    def test_citations_reference_names_and_optional_pages_are_checked(self):
        requirements = {"min_chars": 800, "min_references": 5}
        markdown = assemble_markdown(body(["[1,2,3,4,5]"]), self.sources(), requirements)
        self.assertEqual(markdown.count("## 参考文献"), 1)
        self.assertIn("[5] " + self.names[4], markdown)
        old = body() + "\n\n## 参考文献\n\n[1] 模型猜测的文件名.pdf"
        self.assertNotIn("模型猜测", assemble_markdown(old, self.sources(), requirements))
        self.assertIn("[1, p. 3]", assemble_markdown(body(["[1, p. 3]", "[2]", "[3]", "[4]", "[5]"]), self.sources(), requirements))
        for bad, expected in ((body() + "[6]", "不在指定"), (body() + "[1, p. 99]", "未实际读取"),
                              (body() + "[1]" * 40, "最多 40"), ("# 短文\n[1]", "字符"),
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
            replies = iter([outline(), overview(range(1, 6)), notes(range(1, 6)), comparison(), body()])
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


if __name__ == "__main__":
    unittest.main()
