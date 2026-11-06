"""用本地 HTTP 接收器验证官方 SDK 的真实 OTLP 导出，不调用模型或赛事服务。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import unittest
from unittest.mock import patch

from agenthack import BaseAgent
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from main import main
from src.runtime import ResearchPilotRuntime


class StubAgent(BaseAgent):
    def __init__(self):
        self.seen = None
        self.result = {"case_id": "B-01", "papers": [{"title": "离线测试"}]}
        self.error = None

    async def solve(self, payload):
        self.seen = payload
        if self.error is not None:
            raise self.error
        return self.result


class RuntimeOTLPTests(unittest.TestCase):
    def test_platform_configuration_exports_actual_task_traces(self):
        exports = []

        class Collector(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                trace = ExportTraceServiceRequest()
                trace.ParseFromString(body)
                exports.append((self.path, self.headers.get("X-Collector-Test"), trace))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        collector = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
        thread = threading.Thread(target=collector.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"http://127.0.0.1:{collector.server_port}/v1/traces",
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "X-Collector-Test=offline",
                "AGENTHACK_TELEMETRY_REQUIRED": "true", "AGENTHACK_FLUSH_ON_SOLVE": "true",
                "CHALLENGE_SESSION_ID": "platform-session", "TEAM_ID": "platform-team",
                "AGENT_ID": "platform-agent", "OTEL_SERVICE_NAME": "runtime-offline-test",
            }
            with patch.dict(os.environ, environment, clear=True):
                agent = StubAgent()
                # 仅替换业务 Agent；入口、Runtime、Telemetry 和导出器均使用真实实现。
                with patch("main.ResearchPilotAgent", return_value=agent):
                    with patch("main.dotenv_values", return_value={}):
                        runtime = main()
                with TestClient(runtime.app, raise_server_exceptions=False) as client:
                    self.assertEqual(client.get("/health").json(), {
                        "status": "healthy", "telemetry_configured": True,
                    })
                    payload = {
                        "case_id": "B-01", "function": "Literature_Retrieval", "title": "离线测试题",
                        "input": "测试输入", "materials": ["paper.pdf"], "constraints": ["测试约束"],
                        "expected_output": "论文列表", "output_schema_ref": "Literature_Retrieval",
                    }
                    response = client.post("/solve", json=payload, headers={
                        "X-Research-Task-ID": "platform-task",
                        "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(agent.seen, payload)
                    self.assertEqual(response.json(), agent.result)
                    self.assertIsInstance(response.json(), dict)
                    # 在退出服务前就应收到 Trace，证明 /solve 的 flush 实际生效。
                    self.assertTrue(exports)
                    spans = {}
                    for path, header, trace in exports:
                        self.assertEqual(path, "/v1/traces")
                        self.assertEqual(header, "offline")
                        for resource in trace.resource_spans:
                            attributes = {item.key: item.value.string_value for item in resource.resource.attributes}
                            self.assertEqual(attributes["service.name"], "runtime-offline-test")
                            for scope in resource.scope_spans:
                                for span in scope.spans:
                                    spans[span.name] = span
                    self.assertEqual(set(spans), {"agent.session", "agent.task"})
                    session = spans["agent.session"]
                    attributes = {item.key: item.value.string_value for item in session.attributes}
                    self.assertEqual(attributes["task_id"], "platform-task")
                    self.assertEqual(attributes["task_type"], "Literature_Retrieval")
                    self.assertEqual(attributes["session_id"], "platform-session")
                    self.assertEqual(attributes["team_id"], "platform-team")
                    self.assertEqual(attributes["agent_id"], "platform-agent")
                    self.assertEqual(session.trace_id.hex(), "1" * 32)
                    self.assertEqual(session.parent_span_id.hex(), "2" * 16)
                    self.assertEqual(spans["agent.task"].parent_span_id, session.span_id)
                    self.assertIn("result_size", {item.key for item in spans["agent.task"].attributes})

                    exports.clear()
                    agent.error = ValueError("internal secret")
                    response = client.post("/solve", json=payload, headers={"X-Research-Task-ID": "failed-task"})
                    self.assertEqual(response.status_code, 500)
                    self.assertNotIn("internal secret", response.text)
                    failed_sessions = [
                        span for _, _, trace in exports
                        for resource in trace.resource_spans for scope in resource.scope_spans
                        for span in scope.spans if span.name == "agent.session"
                    ]
                    self.assertEqual(len(failed_sessions), 1)
                    attributes = {item.key: item.value.string_value for item in failed_sessions[0].attributes}
                    self.assertEqual(attributes["task_id"], "failed-task")
                    self.assertEqual(attributes["task_status"], "failure")
        finally:
            collector.shutdown()
            collector.server_close()
            thread.join(timeout=2)

    def test_missing_export_configuration_is_not_reported_as_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = ResearchPilotRuntime(StubAgent())
            with TestClient(runtime.app) as client:
                self.assertEqual(client.get("/health").json(), {
                    "status": "healthy", "telemetry_configured": False,
                })

    def test_required_telemetry_without_endpoint_fails_at_sdk_initialization(self):
        with patch.dict(os.environ, {"AGENTHACK_TELEMETRY_REQUIRED": "true"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
                ResearchPilotRuntime(StubAgent())


if __name__ == "__main__":
    unittest.main()
