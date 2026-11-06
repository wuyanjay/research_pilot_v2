"""临时派题文件与假 handler 验证；不读取真实材料，不调用模型。"""

import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agenthack.telemetry import Telemetry
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.agent import ResearchPilotAgent
from src.dispatcher import Dispatcher, QUESTION_FIELDS
from src.handlers import ChatHandler, RetrievalHandler, SurveyHandler, ReproductionHandler, IdeaHandler
from src.runtime import ResearchPilotRuntime


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.team = "本地队伍"
        self.directory = self.root / "data" / self.team
        (self.directory / "assigned").mkdir(parents=True)
        self.dispatcher = Dispatcher(workspace_root=self.root, team=self.team)
        groups = [
            ("Chat", ["A-100"]),
            ("Literature_Retrieval", ["B-01", "B-02", "B-03"]),
            ("Survey", ["C-01", "C-03"]),
            ("Reproduction", ["D-01", "D-02", "D-05"]),
            ("Idea", ["E-01", "E-02"]),
        ]
        entries = []
        for function, case_ids in groups:
            for case_id in case_ids:
                entries.append({
                    "case_id": case_id, "function": function, "title": f"题目 {case_id}",
                    "question_file": f"data/{self.team}/assigned/{case_id}.json",
                    "question_sha256": "not-read-for-other-cases",
                })
        # 仅包含定位和归属所需元数据，不依赖无关版本或平台配置字段。
        self.dispatch = {
            "team": self.team, "case_ids": [entry["case_id"] for entry in entries], "cases": entries,
        }
        self.payload = self.select("A-100")

    def save_dispatch(self):
        (self.directory / "dispatch.json").write_text(
            json.dumps(self.dispatch, ensure_ascii=False), encoding="utf-8",
        )

    def select(self, case_id):
        entry = next(item for item in self.dispatch["cases"] if item["case_id"] == case_id)
        question = {
            "case_id": case_id, "function": entry["function"], "title": entry["title"],
            "input": "题目要求", "materials": ["完整 论文名.pdf", "relative/实验数据.csv", "/data/papers/paper.pdf"],
            "constraints": ["限制条件"], "expected_output": "要求的答卷", "output_schema_ref": entry["function"],
        }
        self.save_question(question, case_id)
        return question

    def save_question(self, question, case_id="A-100"):
        # 用不同于 HTTP 请求的字段顺序和排版保存 assigned，摘要对应实际原始字节。
        content = json.dumps(dict(reversed(list(question.items()))), ensure_ascii=False, indent=4).encode("utf-8")
        (self.directory / "assigned" / f"{case_id}.json").write_bytes(content)
        entry = next(item for item in self.dispatch["cases"] if item["case_id"] == case_id)
        entry["question_sha256"] = hashlib.sha256(content).hexdigest()
        self.save_dispatch()

    def test_resolve_original_values_and_reads_only_current_question_once(self):
        opened = []
        original_open = Path.open

        def tracked_open(path, *args, **kwargs):
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", tracked_open):
            question = self.dispatcher.resolve(self.payload)
        self.assertEqual(question, self.payload)
        self.assertIsNot(question, self.payload)
        self.assertEqual(question["materials"], self.payload["materials"])
        self.assertEqual(opened, [
            self.directory / "dispatch.json", self.directory / "assigned" / "A-100.json",
        ])
        self.assertEqual(len(list((self.directory / "assigned").iterdir())), 1)

    def test_digest_mismatch_is_rejected_before_parsing(self):
        (self.directory / "assigned" / "A-100.json").write_bytes(b"not even JSON")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.dispatcher.resolve(self.payload)

    def test_conflict_in_each_question_field_is_rejected(self):
        for field in QUESTION_FIELDS:
            with self.subTest(field=field):
                conflicting = deepcopy(self.payload)
                if field == "case_id":
                    conflicting[field] = "A-99"
                elif field in ("materials", "constraints"):
                    conflicting[field].append("conflicting value")
                else:
                    conflicting[field] += " conflicting value"
                with self.assertRaises(ValueError):
                    self.dispatcher.resolve(conflicting)

    def test_current_question_metadata_must_match_dispatch(self):
        for field in ("case_id", "function", "title"):
            with self.subTest(field=field):
                question = deepcopy(self.payload)
                question[field] += " conflict"
                self.save_question(question)
                with self.assertRaisesRegex(ValueError, f"题目文件中的 {field} 与派题记录不一致"):
                    self.dispatcher.resolve(self.payload)

    def test_dispatch_team_unique_ids_counts_and_cases_are_checked(self):
        original = deepcopy(self.dispatch)
        invalid_records = []
        record = deepcopy(original)
        record["team"] = "other-team"
        invalid_records.append(record)
        record = deepcopy(original)
        record["case_ids"][-1] = record["case_ids"][0]
        invalid_records.append(record)
        record = deepcopy(original)
        record["case_ids"][-1] = "D-03"
        record["cases"][-1]["case_id"] = "D-03"
        invalid_records.append(record)
        record = deepcopy(original)
        record["case_ids"].pop()
        invalid_records.append(record)
        record = deepcopy(original)
        record["cases"].pop()
        invalid_records.append(record)
        record = deepcopy(original)
        record["cases"][-1] = deepcopy(record["cases"][0])
        invalid_records.append(record)
        for index, record in enumerate(invalid_records):
            with self.subTest(index=index):
                self.dispatch = record
                self.save_dispatch()
                with self.assertRaises(ValueError):
                    self.dispatcher.resolve(self.payload)

    def test_question_path_cannot_leave_assigned_or_use_wrong_filename(self):
        for path in (
            str(self.directory / "assigned" / "A-100.json"),
            f"data/{self.team}/assigned/../assigned/A-100.json",
            "data/other-team/assigned/A-100.json",
            f"data/{self.team}/assigned/B-01.json",
        ):
            with self.subTest(path=path):
                self.dispatch["cases"][0]["question_file"] = path
                self.save_dispatch()
                with self.assertRaises(ValueError):
                    self.dispatcher.resolve(self.payload)

    def test_symlink_cannot_redirect_question_to_another_team(self):
        path = self.directory / "assigned" / "A-100.json"
        other = self.root / "data" / "other-team" / "assigned" / "A-100.json"
        other.parent.mkdir(parents=True)
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "本队 assigned 目录"):
            self.dispatcher.resolve(self.payload)

    def test_team_configuration_and_workspace_do_not_use_data_path(self):
        with patch.dict(os.environ, {
            "RESEARCH_WORKSPACE_TEAM": "platform-team", "TEAM_NAME": "local-env-team", "DATA_PATH": "/wrong-root",
            "RESEARCH_RESULT_ROOT": "/output",
        }):
            self.assertEqual(Dispatcher().team, "platform-team")
            self.assertEqual(Dispatcher().workspace_root, Path("/output").resolve())
            self.assertEqual(Dispatcher(team=self.team).team, self.team)
            with patch.dict(os.environ, {"RESEARCH_WORKSPACE_TEAM": ""}):
                self.assertEqual(Dispatcher().team, "local-env-team")
        with patch.dict(os.environ, {"RESEARCH_RESULT_ROOT": str(self.root)}):
            self.assertEqual(Dispatcher().workspace_root, self.root)
            self.assertEqual(Dispatcher(workspace_root="/output").workspace_root, Path("/output").resolve())
        for team in ("../other-team", "/tmp", ".", ".."):
            with self.subTest(team=team), self.assertRaises(ValueError):
                Dispatcher(team=team)

    def test_agent_calls_each_matching_handler_with_verified_question(self):
        with patch("src.agent.Dispatcher", return_value=self.dispatcher):
            agent = ResearchPilotAgent()
        expected = [
            ("A-100", "Chat", ChatHandler),
            ("B-01", "Literature_Retrieval", RetrievalHandler),
            ("C-01", "Survey", SurveyHandler),
            ("D-01", "Reproduction", ReproductionHandler),
            ("E-01", "Idea", IdeaHandler),
        ]
        for _, function, handler_class in expected:
            self.assertIsInstance(agent.handlers[function], handler_class)
        mocks = {}
        for function, handler in agent.handlers.items():
            mocked = AsyncMock(return_value={"handler": function})
            handler.handle = mocked
            mocks[function] = mocked
        for case_id, function, _ in expected:
            with self.subTest(function=function):
                payload = self.select(case_id)
                with patch.object(self.dispatcher, "resolve", wraps=self.dispatcher.resolve) as resolve:
                    # 此测试只验证五类路由；正式交付另用真实组件集成验证。
                    with patch.object(agent.delivery, "deliver", return_value=mocks[function].return_value) as deliver:
                        result = asyncio.run(agent.solve(payload))
                        deliver.assert_called_once_with(mocks[function].call_args.args[0], result)
                    resolve.assert_called_once_with(payload)
                self.assertIs(result, mocks[function].return_value)
                mocks[function].assert_awaited_once()
                question = mocks[function].call_args.args[0]
                self.assertEqual(question, payload)
                self.assertIsNot(question, payload)
                self.assertEqual(question["materials"], payload["materials"])

    def test_rejected_input_never_calls_handler_and_stubs_fail_explicitly(self):
        with patch("src.agent.Dispatcher", return_value=self.dispatcher):
            agent = ResearchPilotAgent()
        handler = agent.handlers["Chat"]
        with patch.object(handler, "handle", new_callable=AsyncMock) as mocked:
            payload = {**self.payload, "title": "conflict"}
            with self.assertRaises(ValueError):
                asyncio.run(agent.solve(payload))
            mocked.assert_not_awaited()
        for function, handler in agent.handlers.items():
            if function in ("Chat", "Literature_Retrieval", "Survey"):
                continue
            with self.subTest(handler=type(handler).__name__), self.assertRaises(NotImplementedError):
                asyncio.run(handler.handle(self.payload))

    def test_http_agent_dispatcher_chain_preserves_task_tracing(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.addCleanup(provider.shutdown)
        with patch("src.agent.Dispatcher", return_value=self.dispatcher):
            agent = ResearchPilotAgent()
        result = {"case_id": "A-100", "answer": "测试结果", "clarifying_question": "研究对象是什么？"}
        agent.handlers["Chat"].handle = AsyncMock(return_value=result)
        runtime = ResearchPilotRuntime(agent, telemetry=Telemetry(provider=provider))
        with TestClient(runtime.app, raise_server_exceptions=False) as client:
            response = client.post("/solve", json=self.payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), result)
            spans = exporter.get_finished_spans()
            self.assertEqual({span.name for span in spans}, {"agent.session", "agent.task", "agent.tool"})
            self.assertEqual(json.loads((self.directory / "A-100.json").read_bytes()), response.json())
            tool_span = next(span for span in spans if span.name == "agent.tool")
            self.assertEqual(tool_span.attributes["tool_name"], "write_result_json")
            response = client.post("/solve", json={**self.payload, "title": "secret conflict"})
            self.assertEqual(response.status_code, 500)
            self.assertNotIn("secret conflict", response.text)
        agent.handlers["Chat"].handle.assert_awaited_once()

    def test_http_calls_real_chat_handler_with_verified_question_and_fake_llm(self):
        payload = {**self.payload, "materials": []}
        self.save_question(payload)
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.addCleanup(provider.shutdown)
        with patch("src.agent.Dispatcher", return_value=self.dispatcher):
            agent = ResearchPilotAgent()
        self.assertIs(agent.handlers["Chat"].llm, agent.llm)
        agent.llm.complete = AsyncMock(side_effect=[
            '{"answer":"初次回答","clarifying_question":""}',
            '{"answer":"科研回答","clarifying_question":"研究对象是什么？","case_id":"错误题号"}',
        ])
        runtime = ResearchPilotRuntime(agent, telemetry=Telemetry(provider=provider))
        with TestClient(runtime.app, raise_server_exceptions=False) as client:
            response = client.post("/solve", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "case_id": payload["case_id"], "answer": "科研回答", "clarifying_question": "研究对象是什么？",
        })
        self.assertEqual(agent.llm.complete.await_count, 2)
        self.assertEqual({span.name for span in exporter.get_finished_spans()}, {"agent.session", "agent.task", "agent.tool"})
        self.assertEqual(json.loads((self.directory / "A-100.json").read_bytes()), response.json())

    def test_http_delivery_failure_does_not_return_success(self):
        with patch("src.agent.Dispatcher", return_value=self.dispatcher):
            agent = ResearchPilotAgent()
        agent.llm.complete = AsyncMock(return_value='{"answer":"回答","clarifying_question":"问题？"}')
        provider = TracerProvider()
        self.addCleanup(provider.shutdown)
        runtime = ResearchPilotRuntime(agent, telemetry=Telemetry(provider=provider))
        with patch("src.delivery.result.os.replace", side_effect=PermissionError("private filesystem detail")):
            with TestClient(runtime.app, raise_server_exceptions=False) as client:
                response = client.post("/solve", json=self.payload)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private filesystem detail", response.text)
        self.assertFalse((self.directory / "A-100.json").exists())
        self.assertEqual(list(self.directory.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
