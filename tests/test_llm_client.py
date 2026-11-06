"""真实 OpenAI SDK 配合内存 HTTP 替身，不访问任何网关或模型。"""

import asyncio
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from agenthack import AgentRuntime, BaseAgent
from agenthack.telemetry import Telemetry, task
import httpx
from openai import AsyncOpenAI
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.llm.client import LLMClient, LLMError, LLMTruncatedError


def completion():
    return {
        "id": "offline-response", "object": "chat.completion", "created": 0,
        "model": "platform-model", "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "  科研回答  "},
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {
            "CHALLENGE_LLM_BASE_URL": "https://gateway.invalid/session-route/v1",
            "CHALLENGE_LLM_API_KEY": "offline-key",
            "CHALLENGE_LLM_MODEL": "platform-model",
            "CHALLENGE_SESSION_ID": "session-one",
            "AGENT_LLM_TIMEOUT_S": "2.5",
            "PLATFORM_LLM_URL": "https://unused.invalid",
            "PLATFORM_API_KEY": "unused-key",
            "PLATFORM_LLM_MODEL": "unused-model",
            "AGENT_MODEL_NAME": "old-model",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.requests = []
        self.http_clients = []
        self.client_options = []
        self.reply = lambda request: httpx.Response(200, json=completion())
        self.messages = [{"role": "user", "content": "科研问题"}]

        def respond(request):
            self.requests.append(request)
            return self.reply(request)

        def create_client(**kwargs):
            self.client_options.append(kwargs)
            http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            self.http_clients.append(http_client)
            return AsyncOpenAI(http_client=http_client, **kwargs)

        factory = patch("src.llm.client.AsyncOpenAI", side_effect=create_client)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    async def test_chat_parameters_session_header_and_connection_cleanup(self):
        client = LLMClient()
        self.factory.assert_not_called()
        original_messages = deepcopy(self.messages)
        result = await client.complete(self.messages, max_tokens=128, temperature=0.4)
        self.assertEqual(result, "科研回答")
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://gateway.invalid/session-route/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer offline-key")
        self.assertEqual(request.headers["X-Session-ID"], "session-one")
        self.assertEqual(json.loads(request.content), {
            "model": "platform-model", "messages": self.messages,
            "max_tokens": 128, "temperature": 0.4, "stream": False,
        })
        self.assertEqual(self.messages, original_messages)
        self.assertEqual(self.client_options[0]["timeout"], 2.5)
        self.assertEqual(self.client_options[0]["max_retries"], 0)
        self.assertTrue(self.http_clients[0].is_closed)
        with patch.dict(os.environ, {"CHALLENGE_SESSION_ID": "session-two"}):
            await client.complete(self.messages)
        self.assertEqual(self.requests[1].headers["X-Session-ID"], "session-two")
        self.assertNotIn("max_tokens", json.loads(self.requests[1].content))
        self.assertTrue(all(client.is_closed for client in self.http_clients))

    async def test_compatible_environment_variables(self):
        for model_variable in ("PLATFORM_LLM_MODEL", "AGENT_MODEL_NAME"):
            with self.subTest(model_variable=model_variable):
                with patch.dict(os.environ, {
                    "PLATFORM_LLM_URL": "https://gateway.invalid/v1",
                    "PLATFORM_API_KEY": "compatibility-key",
                    model_variable: "compatibility-model",
                }, clear=True):
                    client = LLMClient()
                    self.assertEqual(client.timeout_s, 900)
                    await client.complete(self.messages)
                    self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer compatibility-key")
                    self.assertNotIn("X-Session-ID", self.requests[-1].headers)
                    self.assertEqual(json.loads(self.requests[-1].content)["model"], "compatibility-model")

    async def test_client_does_not_explicitly_reject_missing_model_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            client = LLMClient()
            self.assertIsNone(client.base_url)
            self.assertIsNone(client._api_key)
            self.assertIsNone(client.model)
        self.factory.assert_not_called()

    async def test_unusable_responses_fail_without_returning_partial_text(self):
        cases = []
        body = completion()
        body["choices"] = []
        cases.append((body, LLMError, "候选回复"))
        for reason, error_class, expected in (
            ("length", LLMTruncatedError, "截断"),
            ("content_filter", LLMError, "未正常结束"),
            (None, LLMError, "未正常结束"),
        ):
            body = completion()
            body["choices"][0]["finish_reason"] = reason
            cases.append((body, error_class, expected))
        for content in ("", "  ", None):
            body = completion()
            body["choices"][0]["message"]["content"] = content
            cases.append((body, LLMError, "空文本"))
        body = completion()
        body["choices"][0]["message"] = None
        cases.append((body, LLMError, "未返回回复消息"))
        body = completion()
        body["choices"][0]["message"]["tool_calls"] = [{
            "id": "unexpected", "type": "function", "function": {"name": "unexpected", "arguments": "{}"},
        }]
        body["choices"][0]["finish_reason"] = "tool_calls"
        cases.append((body, LLMError, "意外的工具调用"))
        body = completion()
        body["choices"][0]["message"]["function_call"] = {"name": "old-function", "arguments": "{}"}
        cases.append((body, LLMError, "意外的工具调用"))
        for body, error_class, expected in cases:
            with self.subTest(expected=expected):
                self.requests.clear()
                self.reply = lambda request: httpx.Response(200, json=body)
                with self.assertRaisesRegex(error_class, expected):
                    await LLMClient().complete(self.messages)
                self.assertEqual(len(self.requests), 1 if error_class is LLMTruncatedError else 4)
        self.assertTrue(all(client.is_closed for client in self.http_clients))

    async def test_refusal_field_is_not_checked_separately(self):
        body = completion()
        body["choices"][0]["message"]["refusal"] = "ignored-field"
        self.reply = lambda request: httpx.Response(200, json=body)
        self.assertEqual(await LLMClient().complete(self.messages), "科研回答")
        self.assertEqual(len(self.requests), 1)

    async def test_mixed_failures_share_three_retries_and_can_succeed_on_fourth_request(self):
        bodies = [completion() for _ in range(3)]
        bodies[0]["choices"] = []
        bodies[1]["choices"][0]["message"]["content"] = ""

        def reply(request):
            if len(self.requests) == 1:
                raise httpx.ReadTimeout("offline timeout", request=request)
            return httpx.Response(200, json=bodies[len(self.requests) - 2])

        self.reply = reply
        original_messages = deepcopy(self.messages)
        self.assertEqual(await LLMClient().complete(self.messages), "科研回答")
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.messages, original_messages)
        for request in self.requests:
            self.assertEqual(json.loads(request.content)["messages"], original_messages)
            self.assertNotIn("max_tokens", json.loads(request.content))
        self.assertTrue(all(client.is_closed for client in self.http_clients))

    async def test_mixed_failures_cannot_reset_retry_budget(self):
        def reply(request):
            if len(self.requests) % 2:
                raise httpx.ReadTimeout("offline timeout", request=request)
            body = completion()
            body["choices"][0]["message"]["content"] = ""
            return httpx.Response(200, json=body)

        self.reply = reply
        with self.assertRaisesRegex(LLMError, "空文本"):
            await LLMClient().complete(self.messages)
        self.assertEqual(len(self.requests), 4)

    async def test_gateway_errors_are_sanitized_and_not_retried(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.reply = lambda request: httpx.Response(status, json={"error": {"message": "secret-upstream-body"}})
                with self.assertRaises(LLMError) as raised:
                    await LLMClient().complete(self.messages)
                self.assertEqual(raised.exception.status_code, status)
                self.assertNotIn("secret-upstream-body", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(len(self.requests), 1)
        for exception_class, expected in ((httpx.ReadTimeout, "超时"), (httpx.ConnectError, "调用失败")):
            with self.subTest(exception=exception_class.__name__):
                self.requests.clear()

                def fail(request):
                    raise exception_class("secret-transport-error", request=request)

                self.reply = fail
                with self.assertRaisesRegex(LLMError, expected) as raised:
                    await LLMClient().complete(self.messages)
                self.assertNotIn("secret-transport-error", str(raised.exception))
                self.assertEqual(len(self.requests), 4 if exception_class is httpx.ReadTimeout else 1)
        self.assertTrue(all(client.is_closed for client in self.http_clients))

    async def test_external_cancellation_propagates_and_closes_connection(self):
        started = asyncio.Event()

        async def wait_for_cancellation(request):
            started.set()
            await asyncio.Event().wait()

        self.reply = wait_for_cancellation
        request = asyncio.create_task(LLMClient().complete(self.messages))
        await started.wait()
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.http_clients[0].is_closed)

    async def test_per_call_options_disable_retries_without_changing_defaults(self):
        client = LLMClient()
        self.reply = lambda request: httpx.Response(200, json={**completion(), "choices": []})
        with self.assertRaises(LLMError) as raised:
            await client.complete(self.messages, max_retries=0, timeout_s=12)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.client_options[0]["timeout"], 12)
        self.requests.clear()
        with self.assertRaises(LLMError):
            await client.complete(self.messages)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.client_options[-1]["timeout"], 2.5)
        self.requests.clear()
        self.reply = lambda request: httpx.Response(401, json={"error": {"message": "private"}})
        with self.assertRaises(LLMError) as raised:
            await client.complete(self.messages, max_retries=0)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(self.requests), 1)

    async def test_official_agenthack_sdk_records_model_call_and_usage(self):
        class StubAgent(BaseAgent):
            async def solve(self, payload):
                return {}

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.addCleanup(provider.shutdown)
        telemetry = Telemetry(provider=provider)
        # 使用官方初始化安装拦截器；SDK 接管追踪，client 不另建追踪系统。
        AgentRuntime(StubAgent(), telemetry=telemetry)

        def reply(request):
            if len(self.requests) == 1:
                raise httpx.ReadTimeout("offline timeout", request=request)
            return httpx.Response(200, json=completion())

        self.reply = reply
        with telemetry.session(task_id="offline-task", task_type="Chat"):
            with task() as operation:
                text = await LLMClient().complete(self.messages)
                operation.result(text)
        spans = exporter.get_finished_spans()
        llm_spans = [span for span in spans if span.name == "agent.llm"]
        self.assertEqual(len(llm_spans), 2)
        successful = next(span for span in llm_spans if span.attributes.get("input_tokens") == 3)
        self.assertEqual(successful.attributes["output_tokens"], 4)
        session = next(span for span in spans if span.name == "agent.session")
        self.assertEqual(session.attributes["llm_call_count"], 2)
        self.assertEqual(len(self.requests), 2)


if __name__ == "__main__":
    unittest.main()
