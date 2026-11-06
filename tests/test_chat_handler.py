"""使用假的 LLM client 验证 A 类生成、格式修复与答卷组装。"""

import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.handlers.chat import ChatHandler
from src.llm.client import LLMClient, LLMError, LLMTruncatedError


def question():
    return {
        "case_id": "A-100", "function": "Chat", "title": "光照与植物生长",
        "input": "解释光照与植物生长的关系，并提出一个关键问题。",
        "materials": [], "constraints": ["区分事实与推测", "使用中文"],
        "expected_output": "科研解释和一个范围限定问题", "output_schema_ref": "Chat",
    }


def valid_reply():
    return json.dumps({
        "answer": "  光照影响光合作用，具体关系取决于植物种类和生长阶段。  ",
        "clarifying_question": "  研究哪一种植物？  ",
    }, ensure_ascii=False)


class ChatHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.llm = Mock(spec=LLMClient)
        self.llm.complete = AsyncMock(return_value=valid_reply())
        self.handler = ChatHandler(self.llm)

    async def test_builds_prompt_and_returns_only_submission_fields(self):
        payload = question()
        original = deepcopy(payload)
        reply = json.loads(valid_reply())
        reply.update({"case_id": "A-01", "metadata": "额外内容"})
        self.llm.complete.return_value = json.dumps(reply, ensure_ascii=False)
        result = await self.handler.handle(payload)
        self.assertEqual(result, {
            "case_id": "A-100",
            "answer": reply["answer"].strip(),
            "clarifying_question": reply["clarifying_question"].strip(),
        })
        self.assertEqual(payload, original)
        self.llm.complete.assert_awaited_once()
        call = self.llm.complete.call_args
        self.assertNotIn("max_tokens", call.kwargs)
        self.assertEqual(call.kwargs["temperature"], 0.2)
        messages = call.args[0]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        for field in ("title", "input", "expected_output"):
            self.assertIn(payload[field], messages[1]["content"])
        for constraint in payload["constraints"]:
            self.assertIn(constraint, messages[1]["content"])

    async def test_invalid_json_is_repaired_by_regenerating_full_reply(self):
        invalid = '```json\n{"answer":"回答"}\n```'
        self.llm.complete.side_effect = [invalid, valid_reply()]
        result = await self.handler.handle(question())
        self.assertEqual(result["case_id"], "A-100")
        self.assertEqual(self.llm.complete.await_count, 2)
        first_messages = self.llm.complete.call_args_list[0].args[0]
        second_messages = self.llm.complete.call_args_list[1].args[0]
        self.assertEqual(len(first_messages), 2)
        self.assertEqual(second_messages[:2], first_messages)
        self.assertEqual(second_messages[2], {"role": "assistant", "content": invalid})
        self.assertIn("不是合法 JSON", second_messages[3]["content"])
        self.assertIn("完整 JSON 对象", second_messages[3]["content"])

    async def test_wrong_object_or_business_fields_trigger_one_repair(self):
        invalid_values = [[], None, "text", {}, {"answer": "回答"}, {"clarifying_question": "问题"}]
        for field in ("answer", "clarifying_question"):
            for invalid in (None, "", "  ", 123, [], {}):
                value = {"answer": "回答", "clarifying_question": "问题"}
                value[field] = invalid
                invalid_values.append(value)
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                self.llm.complete.reset_mock()
                self.llm.complete.side_effect = [json.dumps(invalid), valid_reply()]
                result = await self.handler.handle(question())
                self.assertEqual(result["case_id"], "A-100")
                self.assertEqual(self.llm.complete.await_count, 2)

    async def test_truncation_requests_a_shorter_complete_reply(self):
        self.llm.complete.side_effect = [LLMTruncatedError("截断"), valid_reply()]
        result = await self.handler.handle(question())
        self.assertEqual(result["case_id"], "A-100")
        self.assertEqual(self.llm.complete.await_count, 2)
        messages = self.llm.complete.call_args_list[1].args[0]
        self.assertEqual([message["role"] for message in messages], ["system", "user", "user"])
        self.assertIn("精简回答", messages[-1]["content"])
        self.assertIn("不要续写或补齐", messages[-1]["content"])

    async def test_second_invalid_reply_fails_without_fabricating_answer(self):
        for replies in (
            ["secret invalid reply", "secret invalid reply"],
            ["{}", "{}"],
            [LLMTruncatedError("截断"), LLMTruncatedError("截断")],
        ):
            with self.subTest(replies=replies):
                self.llm.complete.reset_mock()
                self.llm.complete.side_effect = replies
                with self.assertRaisesRegex(ValueError, "修复后仍不合规") as raised:
                    await self.handler.handle(question())
                self.assertNotIn("secret invalid reply", str(raised.exception))
                self.assertEqual(self.llm.complete.await_count, 2)

    async def test_gateway_failures_are_propagated_without_handler_retries(self):
        error = LLMError("网关错误", status_code=500)
        for replies, expected_calls in (([error], 1), (["{}", error], 2)):
            with self.subTest(expected_calls=expected_calls):
                self.llm.complete.reset_mock()
                self.llm.complete.side_effect = replies
                with self.assertRaises(LLMError) as raised:
                    await self.handler.handle(question())
                self.assertIs(raised.exception, error)
                self.assertEqual(self.llm.complete.await_count, expected_calls)

    async def test_cancellation_is_not_treated_as_a_format_error(self):
        self.llm.complete.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.handler.handle(question())
        self.llm.complete.assert_awaited_once()

    async def test_nonempty_materials_do_not_block_model_call(self):
        payload = {**question(), "materials": ["尚未读取的论文.pdf"]}
        original = deepcopy(payload)
        result = await self.handler.handle(payload)
        self.assertEqual(result["case_id"], payload["case_id"])
        self.assertEqual(payload, original)
        self.llm.complete.assert_awaited_once()

    async def test_configured_repair_limit_stops_further_model_calls(self):
        for retries in (0, 2):
            with self.subTest(retries=retries):
                self.llm.complete.reset_mock()
                self.llm.complete.side_effect = ["{}"] * (retries + 1)
                with patch("src.handlers.chat.MAX_REPAIR_RETRIES", retries):
                    with self.assertRaises(ValueError):
                        await self.handler.handle(question())
                self.assertEqual(self.llm.complete.await_count, retries + 1)


if __name__ == "__main__":
    unittest.main()
