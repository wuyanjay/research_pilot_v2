"""临时 PDF、假模型和真实 SDK 会话验证 B 类调用链，不访问外部服务。"""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
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
from src.handlers.retrieval import (
    RetrievalHandler, MAX_BATCH_CHARS, REPAIR_FEEDBACK_CHARS,
    SCREEN_PROMPT, _messages, _dump,
)
from src.llm.client import LLMClient, LLMError, LLMTruncatedError
from src.runtime import ResearchPilotRuntime
from src.tools import PDFTools
from src.tools.paper_reading import read_abstract

ABSTRACT = "Urban vegetation affects LST."
RESULT = "Urban vegetation reduced LST in studied zones."


def question(names):
    return {
        "case_id": "B-99", "function": "Literature_Retrieval", "title": "城市绿地与温度检索",
        "input": "研究城市绿地与地表温度的关系", "materials": names,
        "constraints": ["按相关性排序"], "expected_output": "返回相关论文及理由",
        "output_schema_ref": "Literature_Retrieval",
    }


def criteria():
    return {
        "research_target": {"description": "研究城市绿地与地表温度的关系", "source_quote": "城市绿地与地表温度的关系"},
        "additional_required_conditions": [], "focus_areas": [], "exclusion_conditions": [],
        "quantity_requirement": None,
        "ranking_requirement": {"description": "按相关性排序", "source_quote": "按相关性排序"},
        "search_terms": {"zh": ["城市绿地"], "en": ["vegetation", "LST"]}, "ambiguities": [],
    }


def screening(names, status="candidate", quote=ABSTRACT):
    return {"papers": [{
        "filename": name, "rank": index + 1, "status": status, "reason": "摘要提供了主题判断线索",
        "questions": [] if status == "excluded" else [{"question": "是否有植被与温度的自身研究结果？", "keywords": ["vegetation", "LST"]}],
        "evidence": [{"page": 1, "quote": quote}],
    } for index, name in enumerate(names)]}


def ranking(names, include=True):
    return {"papers": [{
        "filename": name, "include": include, "reason": "正文比较了植被区域的地表温度",
        "evidence": [{"page": 3, "quote": RESULT}] if include else [],
    } for name in names]}


def write_pdf(path, texts=None):
    if texts is None:
        texts = ["Title\nAbstract: " + ABSTRACT + "\nKeywords: urban; LST\n1. Introduction",
                 "Background information.", "3. Results\n" + RESULT, "4. Discussion\nStudy limitations."]
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page()
            page.insert_text((40, 70), text, fontsize=12)
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


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.papers = self.root / "papers"
        self.papers.mkdir()
        self.names = ["第一篇.pdf", "第二篇.pdf"]
        for name in self.names:
            write_pdf(self.papers / name)
        self.question = question(["论文文件夹内全部 PDF"])
        provider = TracerProvider()
        self.addCleanup(provider.shutdown)
        self.exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        session = Telemetry(provider=provider).session(task_id="retrieval-offline", task_type="Literature_Retrieval")
        session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)

    def handler(self, replies):
        llm = FakeLLM(replies)
        return RetrievalHandler(llm, papers_directory=self.papers, work_directory=self.root / "work")

    async def test_real_pdf_flow_preserves_materials_and_reranks_globally(self):
        original = deepcopy(self.question)
        handler = self.handler([criteria(), screening(self.names), ranking(self.names[::-1])])
        result = await handler.handle(self.question)
        self.assertEqual(result, {"case_id": "B-99", "papers": [
            {"filename": name, "reason": "正文比较了植被区域的地表温度"} for name in self.names[::-1]
        ]})
        self.assertEqual(self.question, original)
        self.assertEqual(len(handler.llm.calls), 3)
        for messages, options in handler.llm.calls:
            self.assertEqual(options["max_retries"], 0)
            self.assertIn("timeout_s", options)
            self.assertLessEqual(options["timeout_s"], 90)
            self.assertEqual(options["max_tokens"], 4096)
            self.assertIn(self.question["input"], messages[1]["content"])
        screening_input = json.loads(handler.llm.calls[1][0][1]["content"].split("输入：\n", 1)[1])
        self.assertEqual(screening_input["papers"][0]["path"], str(self.papers / self.names[0]))
        self.assertEqual(screening_input["papers"][0]["abstract_status"], "found")
        self.assertIn("正文", handler.llm.calls[2][0][1]["content"])
        self.assertEqual(len(list((self.root / "work/pdf_cache").glob("*.json"))), 2)

    async def test_invalid_screening_record_is_repaired_instead_of_failing_stage(self):
        bad_screen = screening(self.names)
        bad_screen["papers"][0]["filename"] = "未授权.pdf"
        bad_rank = ranking(self.names)
        bad_rank["papers"][0]["reason"] = " "
        handler = self.handler(["not json", criteria(), bad_screen, bad_rank])
        result = await handler.handle(self.question)
        self.assertEqual(len(result["papers"]), 2)
        self.assertEqual(len(handler.llm.calls), 4)
        self.assertEqual(handler.llm.calls[1][0][-1]["role"], "user")
        self.assertIn("上一份回复不合规", handler.llm.calls[1][0][-1]["content"])

    async def test_unverified_or_missing_evidence_does_not_block_delivery_result(self):
        screened = screening(self.names)
        screened["papers"][0]["evidence"] = [{"page": 999, "quote": "未经过原文核验的说明"}]
        del screened["papers"][1]["evidence"]
        ranked = ranking(self.names)
        ranked["papers"][0]["evidence"] = [{"page": 999, "quote": "可以概述，不要求逐字摘录"}]
        del ranked["papers"][1]["evidence"]
        handler = self.handler([criteria(), screened, ranked])
        result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 3)
        self.assertEqual([paper["filename"] for paper in result["papers"]], self.names)
        self.assertTrue(all(set(paper) == {"filename", "reason"} for paper in result["papers"]))

    async def test_screening_retry_is_independent_per_batch(self):
        handler = self.handler([
            criteria(),
            {"not_papers": []}, screening(self.names[:1]),
            {"not_papers": []}, screening(self.names[1:]),
            ranking(self.names[::-1]),
        ])
        with patch("src.handlers.retrieval.MAX_BATCH_PAPERS", 1):
            result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 6)
        self.assertEqual([paper["filename"] for paper in result["papers"]], self.names[::-1])

    async def test_empty_screening_batch_is_accepted_without_retry(self):
        handler = self.handler([
            criteria(),
            {"papers": []},
            screening(self.names[1:]),
            ranking(self.names[1:]),
        ])
        with patch("src.handlers.retrieval.MAX_BATCH_PAPERS", 1):
            result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 4)
        self.assertEqual([paper["filename"] for paper in result["papers"]], self.names[1:])

    async def test_screening_json_code_fence_is_accepted(self):
        fenced = "```json\n" + json.dumps(screening(self.names), ensure_ascii=False) + "\n```"
        handler = self.handler([criteria(), fenced, ranking(self.names)])
        result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 3)
        self.assertEqual(len(result["papers"]), 2)

    async def test_successful_batch_is_not_replayed_when_next_batch_is_repaired(self):
        handler = self.handler([criteria(), screening(self.names[:1]), "bad json", screening(self.names[1:]), ranking(self.names[::-1])])
        with patch("src.handlers.retrieval.MAX_BATCH_PAPERS", 1):
            result = await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 5)
        self.assertEqual(result["papers"][0]["filename"], self.names[1])
        for index, expected in ((1, self.names[0]), (2, self.names[1]), (3, self.names[1])):
            batch = json.loads(handler.llm.calls[index][0][1]["content"].split("输入：\n", 1)[1])["papers"]
            self.assertEqual([paper["filename"] for paper in batch], [expected])

    async def test_only_retryable_llm_errors_are_retried_and_cancellation_propagates(self):
        for error in (LLMError("模型网关请求超时", retryable=True), LLMTruncatedError("截断")):
            handler = self.handler([error, criteria(), screening(self.names), ranking(self.names)])
            self.assertEqual(len((await handler.handle(self.question))["papers"]), 2)
            self.assertEqual(len(handler.llm.calls), 4)

        # 非重试错误不再打穿整题：判断依据用默认值，初筛失败批次降级为 pending。
        handler = self.handler([LLMError("状态错误", status_code=401)] * 3)
        result = await handler.handle(self.question)
        self.assertEqual(result, {"case_id": "B-99", "papers": []})
        self.assertEqual(len(handler.llm.calls), 3)

        handler = self.handler([asyncio.CancelledError()])
        with self.assertRaises(asyncio.CancelledError):
            await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 1)

    async def test_missing_papers_directory_is_not_retried_or_converted_to_empty_answer(self):
        handler = self.handler([criteria()])
        handler.papers_directory = self.root / "missing-papers"
        with self.assertRaises(FileNotFoundError):
            await handler.handle(self.question)
        self.assertEqual(len(handler.llm.calls), 1)

    def test_abstract_after_cover_and_across_pages_retains_physical_page_numbers(self):
        path = self.papers / "跨页.pdf"
        write_pdf(path, ["Cover", "Abstract: Urban vegetation", "affects surface temperature.", "Keywords: LST\n1. Introduction"])
        tools = PDFTools([path.name], self.papers)
        summary = read_abstract(tools, tools.list_papers()[0])
        self.assertEqual(summary["abstract_status"], "found")
        self.assertEqual([page["page"] for page in summary["pages"]], [2, 3])
        self.assertNotIn("Keywords", "".join(page["text"] for page in summary["pages"]))
        self.assertEqual(summary["front_matter"][0]["text"].strip(), "Cover")

    async def test_publication_information_remains_available_for_date_conditions(self):
        (self.papers / self.names[1]).unlink()
        path = self.papers / self.names[0]
        path.unlink()
        write_pdf(path, ["Published: 2019\nAbstract: " + ABSTRACT + "\nKeywords: urban; LST",
                         "Background", "3. Results\n" + RESULT])
        dated_question = question([self.names[0]])
        dated_question["constraints"].append("只检索2020年及之后发表的论文")
        dated_criteria = criteria()
        dated_criteria["exclusion_conditions"] = ["排除2020年之前发表的论文"]
        excluded = screening([self.names[0]], "excluded", quote="Published: 2019")
        handler = self.handler([dated_criteria, excluded])
        result = await handler.handle(dated_question)
        self.assertEqual(result["papers"], [])
        self.assertIn("Published: 2019", handler.llm.calls[1][0][1]["content"])

    async def test_unlocated_abstract_is_retained_and_cannot_be_excluded(self):
        (self.papers / self.names[1]).unlink()
        name = self.names[0]
        (self.papers / name).unlink()
        write_pdf(self.papers / name, ["Cover", "Study purpose", "3. Results\n" + RESULT, "Discussion"])
        excluded = screening([name], "excluded", quote="Cover")
        handler = self.handler([criteria(), excluded, ranking([name])])
        result = await handler.handle(question([name]))
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(len(handler.llm.calls), 3)
        self.assertIn("not_found", handler.llm.calls[1][0][1]["content"])

    def test_abstract_scan_is_bounded_and_incomplete_content_is_not_fabricated(self):
        path = self.papers / "不完整摘要.pdf"
        write_pdf(path, ["Abstract: actual beginning"] + ["continued actual text"] * 7)
        tools = PDFTools([path.name], self.papers)
        summary = read_abstract(tools, tools.list_papers()[0])
        self.assertEqual(summary["abstract_status"], "incomplete")
        self.assertEqual([page["page"] for page in summary["pages"]], list(range(1, 7)))

    def test_scanned_abstract_uses_real_ocr_and_keeps_source_page(self):
        path = self.papers / "扫描摘要.pdf"
        with pymupdf.open() as source:
            page = source.new_page()
            page.insert_text((40, 100), "Abstract: Urban vegetation affects LST.", fontsize=18)
            page.insert_text((40, 160), "Keywords: urban; LST", fontsize=18)
            image = page.get_pixmap(dpi=200).tobytes("png")
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_image(page.rect, stream=image)
            document.save(path)
        tools = PDFTools([path.name], self.papers)
        summary = read_abstract(tools, tools.list_papers()[0])
        self.assertEqual(summary["abstract_status"], "found")
        self.assertEqual(summary["pages"][0]["page"], 1)
        self.assertEqual(summary["pages"][0]["method"], "ocr")
        self.assertIn("vegetation", summary["pages"][0]["text"].lower())

    def test_batch_limits_include_prompts_and_oversized_paper_remains_pending(self):
        handler = self.handler([])
        papers = [{"filename": f"{index}.pdf", "path": f"/data/papers/{index}.pdf", "total_pages": 3,
                   "abstract_status": "found", "pages": [{"page": 1, "text": "x" * 10_000}]} for index in range(5)]
        batches, oversized = handler._batches(self.question, criteria(), papers)
        self.assertEqual([len(batch) for batch in batches], [3, 2])
        self.assertEqual(oversized, [])
        for batch in batches:
            self.assertLessEqual(len(_dump(_messages(SCREEN_PROMPT, self.question, criteria=criteria(), papers=batch))) + REPAIR_FEEDBACK_CHARS, MAX_BATCH_CHARS)
        papers[0]["pages"][0]["text"] = "x" * 50_000
        _, oversized = handler._batches(self.question, criteria(), papers)
        self.assertEqual([paper["filename"] for paper in oversized], ["0.pdf"])
        small = [{**papers[1], "filename": f"{index}.pdf"} for index in range(11)]
        with patch("src.handlers.retrieval.MAX_BATCH_CHARS", 1_000_000):
            batches, _ = handler._batches(self.question, criteria(), small)
        self.assertEqual([len(batch) for batch in batches], [10, 1])

    async def test_no_keyword_hit_uses_abstract_pages_and_does_not_preemptively_exclude(self):
        screened = screening(self.names)
        for record in screened["papers"]:
            record["questions"][0]["keywords"] = ["no-such-word"]
        empty_terms = criteria()
        empty_terms["search_terms"] = {"zh": ["不存在词"], "en": ["no-such-word"]}
        handler = self.handler([empty_terms, screened, ranking(self.names)])
        result = await handler.handle(self.question)
        self.assertEqual(len(result["papers"]), 2)
        final_input = json.loads(handler.llm.calls[2][0][1]["content"].split("输入：\n", 1)[1])
        self.assertEqual([page["page"] for page in final_input["evidence"][0]["pages"]], [1])

    async def test_duplicate_search_pages_are_read_once_for_final_evidence(self):
        screened = screening(self.names)
        for record in screened["papers"]:
            record["questions"].append({"question": "再核对温度结果", "keywords": ["LST"]})
        handler = self.handler([criteria(), screened, ranking(self.names)])
        await handler.handle(self.question)
        payload = json.loads(handler.llm.calls[2][0][1]["content"].split("输入：\n", 1)[1])
        for paper in payload["evidence"]:
            self.assertEqual([page["page"] for page in paper["pages"]], [3])
            self.assertEqual(paper["matched_keywords"]["3"], ["vegetation", "LST"])

    async def test_explicit_exclusion_skips_reranking_and_quantity_is_applied_after_global_rank(self):
        handler = self.handler([criteria(), screening(self.names, "excluded")])
        self.assertEqual(await handler.handle(self.question), {"case_id": "B-99", "papers": []})
        self.assertEqual(len(handler.llm.calls), 2)
        limited_question = {**self.question, "constraints": ["按相关性排序", "最多返回1篇"]}
        limited_criteria = criteria()
        limited_criteria["quantity_requirement"] = {"mode": "at_most", "count": 1, "source_quote": "最多返回1篇"}
        handler = self.handler([limited_criteria, screening(self.names), ranking(self.names[::-1])])
        result = await handler.handle(limited_question)
        self.assertEqual([item["filename"] for item in result["papers"]], self.names[1:])

    async def test_exhausted_time_budget_returns_empty_answer_without_starting_request(self):
        handler = self.handler([])
        with patch("src.handlers.retrieval.time.monotonic", side_effect=[0] + [600] * 40):
            result = await handler.handle(self.question)
        self.assertEqual(result, {"case_id": "B-99", "papers": []})
        self.assertEqual(handler.llm.calls, [])

    async def test_body_read_failure_propagates_without_model_repair(self):
        handler = self.handler([criteria(), screening(self.names)])
        error = PermissionError("材料不可读")
        with patch("src.handlers.retrieval.PDFTools.search_pdf_text", side_effect=error):
            with self.assertRaises(PermissionError) as raised:
                await handler.handle(self.question)
        self.assertIs(raised.exception, error)
        self.assertEqual(len(handler.llm.calls), 2)

    async def test_missing_ranking_records_require_repair_instead_of_empty_success(self):
        handler = self.handler([criteria(), screening(self.names), {"papers": []}, ranking(self.names, include=False)])
        result = await handler.handle(self.question)
        self.assertEqual(result, {"case_id": "B-99", "papers": []})
        self.assertEqual(len(handler.llm.calls), 4)

    async def test_screening_normalizes_rank_questions_and_filename(self):
        reply = {"papers": [
            {"filename": "/tmp/" + self.names[0], "rank": "1", "status": "Candidate",
             "reason": "ok", "questions": []},
            {"filename": self.names[1], "rank": 2.0, "status": "pending", "reason": ""},
        ]}
        handler = self.handler([criteria(), reply, ranking(self.names)])
        result = await handler.handle(self.question)
        self.assertEqual(len(result["papers"]), 2)
        self.assertEqual(len(handler.llm.calls), 3)
        screening_input = json.loads(handler.llm.calls[1][0][1]["content"].split("输入：\n", 1)[1])
        self.assertEqual([paper["filename"] for paper in screening_input["papers"]], self.names)

    async def test_large_library_is_funneled_before_abstract_reading(self):
        class FakeTools:
            def __init__(self, items):
                self.items = items

            def list_papers(self):
                return [dict(item) for item in self.items]

            def quick_meta(self, path):
                return {
                    "filename": Path(path).name, "path": path,
                    "title": "urban green space study",
                    "snippet": "urban green space and temperature", "total_pages": 1,
                }

            def search_pdf_text(self, paths, keywords, limit=20, *, ocr=True):
                return []

            def read_pdf_pages(self, path, pages=None, *, ocr=True):
                return {
                    "filename": Path(path).name, "path": path, "total_pages": 1,
                    "pages": [{"page": 1, "text": "urban green space", "method": "text"}],
                }

        class DynamicLLM:
            timeout_s = 900

            def __init__(self):
                self.calls = []

            async def complete(self, messages, **options):
                self.calls.append((deepcopy(messages), options))
                content = messages[1]["content"]
                if "整理检索判断依据" in content:
                    return json.dumps(criteria(), ensure_ascii=False)
                if "选出最可能与题目相关" in content:
                    return json.dumps({"selected": list(range(1, 41))})
                if "摘要初筛" in content:
                    payload = json.loads(content.split("输入：\n", 1)[1])
                    return json.dumps({"papers": [
                        {"filename": paper["filename"], "rank": index + 1,
                         "status": "candidate", "reason": "摘要匹配",
                         "questions": [{"question": "正文是否覆盖主题？", "keywords": ["urban"]}]}
                        for index, paper in enumerate(payload["papers"])
                    ]}, ensure_ascii=False)
                if "根据提供的正文" in content:
                    payload = json.loads(content.split("输入：\n", 1)[1])
                    return json.dumps({"papers": [
                        {"filename": item["filename"], "include": True, "reason": "正文匹配"}
                        for item in payload["evidence"]
                    ]}, ensure_ascii=False)
                raise AssertionError("unexpected prompt: " + content[:80])

        items = [
            {"filename": f"paper_{index:03d}.pdf", "path": f"/papers/paper_{index:03d}.pdf"}
            for index in range(150)
        ]
        llm = DynamicLLM()
        handler = RetrievalHandler(llm, papers_directory=self.papers,
                                   work_directory=self.root / "work")
        read_names = []

        def fake_read_abstract(tools, paper, *, max_pages=6):
            read_names.append(paper["filename"])
            return {
                **paper, "total_pages": 1, "abstract_status": "found", "front_matter": [],
                "pages": [{"page": 1, "text": "urban green space and temperature", "method": "text"}],
            }

        with patch("src.handlers.retrieval.PDFTools", return_value=FakeTools(items)):
            with patch("src.handlers.retrieval_prefilter.read_abstract", side_effect=fake_read_abstract):
                result = await handler.handle(self.question)

        self.assertEqual(len(read_names), 40)
        self.assertEqual(len(llm.calls), 7)
        title_payload = json.loads(llm.calls[1][0][1]["content"].split("输入：\n", 1)[1])
        self.assertEqual(len(title_payload["papers"]), 80)
        self.assertEqual(len(result["papers"]), 30)


class RetrievalHTTPTests(unittest.TestCase):
    def test_eight_field_http_request_runs_real_dispatch_handler_delivery_and_tool_tracing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            papers = root / "input/papers"
            papers.mkdir(parents=True)
            names = ["本地论文.pdf", "另一篇论文.pdf"]
            for name in names:
                write_pdf(papers / name)
            payload = question(["论文文件夹内全部 PDF"])
            directory = root / "output/data/测试队"
            (directory / "assigned").mkdir(parents=True)
            raw = json.dumps(dict(reversed(list(payload.items()))), ensure_ascii=False, indent=4).encode()
            (directory / "assigned/B-99.json").write_bytes(raw)
            entries = []
            for prefix, count in (("A", 1), ("B", 3), ("C", 2), ("D", 3), ("E", 2)):
                for index in range(count):
                    case_id = "B-99" if prefix == "B" and index == 0 else f"{prefix}-{index + 1:02d}"
                    entries.append({"case_id": case_id, "function": "Literature_Retrieval" if prefix == "B" else "unused",
                                    "title": payload["title"] if case_id == "B-99" else "unused",
                                    "question_file": f"data/测试队/assigned/{case_id}.json",
                                    "question_sha256": hashlib.sha256(raw).hexdigest() if case_id == "B-99" else "not-read"})
            (directory / "dispatch.json").write_text(json.dumps({"team": "测试队", "case_ids": [item["case_id"] for item in entries], "cases": entries}), encoding="utf-8")
            replies = iter([criteria(), screening(names), ranking(names)])
            gateway_requests = []
            gateway_timeouts = []

            def respond(request):
                gateway_requests.append(json.loads(request.content))
                return httpx.Response(200, json={
                    "id": "offline", "object": "chat.completion", "created": 0, "model": "offline",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(next(replies), ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                })

            def create_client(**options):
                gateway_timeouts.append(options["timeout"])
                return AsyncOpenAI(http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)), **options)
            exporter = InMemorySpanExporter()
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            self.addCleanup(provider.shutdown)
            with patch.dict(os.environ, {"DATA_PATH": str(root / "input"), "RESEARCH_RESULT_ROOT": str(root / "output"), "RESEARCH_WORKSPACE_TEAM": "测试队",
                                         "CHALLENGE_LLM_BASE_URL": "https://offline.invalid/v1", "CHALLENGE_LLM_API_KEY": "offline-key", "CHALLENGE_LLM_MODEL": "offline", "AGENT_LLM_TIMEOUT_S": "137"}):
                agent = ResearchPilotAgent()
                runtime = ResearchPilotRuntime(agent, telemetry=Telemetry(provider=provider))
                with patch("src.llm.client.AsyncOpenAI", side_effect=create_client):
                    with TestClient(runtime.app, raise_server_exceptions=False) as client:
                        response = client.post("/solve", json=payload, headers={"X-Research-Task-ID": "b-offline-http"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"case_id": "B-99", "papers": [
                {"filename": name, "reason": "正文比较了植被区域的地表温度"} for name in names
            ]})
            self.assertEqual(json.loads((directory / "B-99.json").read_bytes()), response.json())
            self.assertEqual(len(gateway_requests), 3)
            self.assertEqual(len(gateway_timeouts), 3)
            self.assertTrue(all(0 < timeout <= 90 for timeout in gateway_timeouts))
            screening_input = json.loads(gateway_requests[1]["messages"][1]["content"].split("输入：\n", 1)[1])
            self.assertEqual({paper["path"] for paper in screening_input["papers"]}, {str(papers / name) for name in names})
            self.assertEqual((directory / "assigned/B-99.json").read_bytes(), raw)
            self.assertEqual(list((directory / "assigned").iterdir()), [directory / "assigned/B-99.json"])
            spans = exporter.get_finished_spans()
            self.assertTrue({"agent.session", "agent.task", "agent.tool"}.issubset({span.name for span in spans}))
            tool_spans = [span for span in spans if span.name == "agent.tool"]
            self.assertTrue({"list_papers", "read_pdf_pages", "search_pdf_text", "write_result_json"}.issubset({span.attributes["tool_name"] for span in tool_spans}))
            self.assertEqual(len({span.context.trace_id for span in tool_spans}), 1)
            model_spans = [span for span in spans if span.name == "agent.llm"]
            self.assertEqual(len(model_spans), 3)
            self.assertEqual({span.context.trace_id for span in model_spans}, {tool_spans[0].context.trace_id})
            self.assertFalse(list(directory.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
