"""使用真实 SDK 与内存导出器离线验证 HTTP 边界，不调用模型。"""

import asyncio
import inspect
import os
import threading
import unittest
from unittest.mock import patch

from agenthack import AgentRuntime, BaseAgent
from agenthack.telemetry import Telemetry
from fastapi import Body, HTTPException, Request
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from main import main
from src.agent import ResearchPilotAgent
from src.runtime import ResearchPilotRuntime, sdk_traced


def question():
    return {
        "case_id": "B-01", "function": "Literature_Retrieval", "title": "测试题目",
        "input": "检索要求", "materials": ["测试材料"], "constraints": ["测试约束"],
        "expected_output": "论文列表", "output_schema_ref": "Literature_Retrieval",
    }


class StubAgent(BaseAgent):
    def __init__(self):
        self.seen = None
        self.result = {"case_id": "B-01", "papers": [{"title": "示例论文"}]}
        self.error = None

    async def solve(self, payload):
        self.seen = payload
        if self.error is not None:
            raise self.error
        return self.result


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "AGENTHACK_FLUSH_ON_SOLVE": "true", "CHALLENGE_SESSION_ID": "model-session",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(self.provider.shutdown)
        self.telemetry = Telemetry(provider=self.provider)
        self.agent = StubAgent()
        self.runtime = ResearchPilotRuntime(self.agent, telemetry=self.telemetry)
        self.client = TestClient(self.runtime.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def endpoint(self):
        return next(route.endpoint for route in self.runtime.app.routes if route.path == "/solve")

    def request(self):
        return Request({"type": "http", "headers": [(b"x-research-task-id", b"transport-id")]})

    def test_health_and_direct_answer_with_real_sdk_trace(self):
        self.assertEqual(self.client.get("/health").json(), {
            "status": "healthy", "telemetry_configured": True,
        })
        payload = question()
        response = self.client.post("/solve", json=payload, headers={
            "X-Research-Task-ID": "transport-id",
            "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.agent.seen, payload)
        self.assertEqual(response.json(), self.agent.result)
        self.assertNotIn("answer", response.json())
        spans = {span.name: span for span in self.exporter.get_finished_spans()}
        self.assertEqual(set(spans), {"agent.session", "agent.task"})
        session = spans["agent.session"]
        self.assertEqual(session.attributes["task_id"], "transport-id")
        self.assertEqual(session.attributes["task_type"], payload["function"])
        self.assertEqual(session.attributes["session_id"], "model-session")
        self.assertEqual(session.parent.span_id, int("2222222222222222", 16))
        self.assertIn("result_size", spans["agent.task"].attributes)

    def test_original_objects_and_no_case_id_fallback(self):
        payload = question()
        result = asyncio.run(self.endpoint()(self.request(), payload))
        self.assertIs(self.agent.seen, payload)
        self.assertIs(result, self.agent.result)
        self.exporter.clear()
        response = self.client.post("/solve", json=payload)
        self.assertEqual(response.status_code, 200)
        for span in self.exporter.get_finished_spans():
            self.assertNotIn("task_id", span.attributes)

    def test_fastapi_signature_and_json_object_only(self):
        signature = inspect.signature(self.endpoint())
        self.assertEqual(list(signature.parameters), ["http_request", "payload"])
        self.assertIs(signature.parameters["http_request"].annotation, Request)
        schema = self.client.get("/openapi.json").json()["paths"]["/solve"]["post"]
        self.assertNotIn("parameters", schema)
        self.assertEqual(schema["requestBody"]["content"]["application/json"]["schema"]["type"], "object")
        # 不在 HTTP 层实施八字段业务校验。
        self.assertEqual(self.client.post("/solve", json={"extra": "untouched"}).status_code, 200)
        self.assertEqual(self.agent.seen, {"extra": "untouched"})
        for body in ("[]", "null", '"secret-value"', '{"secret-value":'):
            with self.subTest(body=body):
                response = self.client.post("/solve", content=body, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("secret-value", response.text)

    def test_forward_annotations_are_resolved(self):
        async def route(http_request: "Request", payload: "dict" = Body(...)) -> "dict":
            return payload

        wrapped = sdk_traced(self.telemetry)(route)
        self.assertEqual(wrapped.__name__, route.__name__)
        self.assertIs(inspect.signature(wrapped).parameters["http_request"].annotation, Request)
        self.assertIs(wrapped.__annotations__["http_request"], Request)
        self.assertIs(wrapped.__annotations__["return"], dict)

    def test_errors_propagate_and_http_response_is_sanitized(self):
        for error in (ValueError("secret-value"), HTTPException(409, detail="secret-value")):
            with self.subTest(error=type(error).__name__):
                self.agent.error = error
                with self.assertRaises(type(error)) as raised:
                    asyncio.run(self.endpoint()(self.request(), question()))
                self.assertIs(raised.exception, error)
                response = self.client.post("/solve", json=question())
                self.assertEqual(response.status_code, 409 if isinstance(error, HTTPException) else 500)
                self.assertNotIn("secret-value", response.text)

    def test_flush_after_context_exit_and_disabled_flush(self):
        def flush():
            self.assertEqual(len(self.exporter.get_finished_spans()), 2)

        with patch.object(self.telemetry, "flush", side_effect=flush) as mocked:
            self.assertEqual(self.client.post("/solve", json=question()).status_code, 200)
            mocked.assert_called_once()
        with patch.dict(os.environ, {"AGENTHACK_FLUSH_ON_SOLVE": "false"}):
            with patch.object(self.telemetry, "flush") as mocked:
                self.assertEqual(self.client.post("/solve", json=question()).status_code, 200)
                mocked.assert_not_called()

    def test_flush_failure_preserves_success_and_original_exception(self):
        with patch.object(self.telemetry, "flush", side_effect=RuntimeError("export secret")):
            self.assertEqual(self.client.post("/solve", json=question()).json(), self.agent.result)
            error = ValueError("business secret")
            self.agent.error = error
            with self.assertRaises(ValueError) as raised:
                asyncio.run(self.endpoint()(self.request(), question()))
            self.assertIs(raised.exception, error)

    def test_flush_timeout_is_independent_and_preserves_result(self):
        released = threading.Event()

        async def check():
            result = await self.endpoint()(self.request(), question())
            self.assertIs(result, self.agent.result)
            # 刷新线程仍在等待时路由已返回，证明上限作用于刷新等待。
            self.assertFalse(released.is_set())
            released.set()

        try:
            with patch.object(self.telemetry, "flush", side_effect=lambda: released.wait(5)):
                asyncio.run(check())
        finally:
            released.set()

    def test_external_cancellation_propagates_during_solve_and_flush(self):
        async def check_during_solve():
            started = asyncio.Event()

            async def solve(payload):
                started.set()
                await asyncio.Event().wait()

            self.agent.solve = solve
            request = asyncio.create_task(self.endpoint()(self.request(), question()))
            await started.wait()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request

        asyncio.run(check_during_solve())
        self.agent.solve = StubAgent().solve
        released = threading.Event()

        async def check_during_flush():
            started = asyncio.Event()
            loop = asyncio.get_running_loop()

            def flush():
                loop.call_soon_threadsafe(started.set)
                released.wait(5)

            with patch.object(self.telemetry, "flush", side_effect=flush):
                request = asyncio.create_task(self.endpoint()(self.request(), question()))
                await started.wait()
                request.cancel()
                try:
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                finally:
                    released.set()

        asyncio.run(check_during_flush())

    def test_sdk_lifecycle_and_entrypoint(self):
        self.assertIs(ResearchPilotRuntime.__init__, AgentRuntime.__init__)
        self.assertIs(ResearchPilotRuntime.run, AgentRuntime.run)
        with patch.object(self.telemetry, "flush") as flush:
            with patch.object(self.telemetry, "shutdown") as shutdown:
                with TestClient(self.runtime.app) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
                flush.assert_called_once()
                shutdown.assert_called_once()
        with patch("main.ResearchPilotRuntime", return_value=self.runtime) as runtime:
            self.assertIs(main(), self.runtime)
            self.assertIsInstance(runtime.call_args.args[0], ResearchPilotAgent)
        agent = ResearchPilotAgent()
        stub_question = {**question(), "function": "Idea"}
        with patch.object(agent.dispatcher, "resolve", return_value=stub_question):
            with self.assertRaises(NotImplementedError):
                asyncio.run(agent.solve(stub_question))


if __name__ == "__main__":
    unittest.main()
