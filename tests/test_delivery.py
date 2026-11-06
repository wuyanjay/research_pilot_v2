"""在临时工作区和真实 SDK 内存会话中验证正式答卷交付。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agenthack.telemetry import Telemetry
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.delivery import ResultDelivery


def answer(case_id="A-99"):
    return {"case_id": case_id, "answer": "光照会影响植物生长。", "clarifying_question": "研究哪种植物？"}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.team = "测试队伍"
        self.directory = self.root / "data" / self.team
        self.delivery = ResultDelivery(self.root, self.team)
        self.question = {"case_id": "A-99", "function": "Chat"}
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(provider.shutdown)
        telemetry = Telemetry(provider=provider)
        session = telemetry.session(task_id="delivery-test", task_type="Chat")
        session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)

    def test_success_writes_utf8_returns_original_result_and_records_tool(self):
        result = answer()
        original = deepcopy(result)
        returned = self.delivery.deliver(self.question, result)
        self.assertIs(returned, result)
        target = self.directory / "A-99.json"
        self.assertTrue(target.stat().st_size > 0)
        self.assertIn("植物生长", target.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(target.read_bytes()), result)
        self.assertEqual(result, original)
        self.assertEqual(list(self.directory.iterdir()), [target])
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, "agent.tool")
        self.assertEqual(spans[0].attributes["tool_name"], "write_result_json")

    def test_different_case_ids_use_different_files_and_same_case_can_replace(self):
        first = answer()
        second = answer("A-100")
        self.delivery.deliver(self.question, first)
        self.delivery.deliver({"case_id": "A-100", "function": "Chat"}, second)
        updated = {**first, "answer": "更新后的回答"}
        self.delivery.deliver(self.question, updated)
        self.assertEqual(json.loads((self.directory / "A-99.json").read_bytes()), updated)
        self.assertEqual(json.loads((self.directory / "A-100.json").read_bytes()), second)
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_invalid_results_are_rejected_before_creating_files(self):
        invalid_results = [None, [], {"case_id": "A-99"}, {**answer(), "metadata": {}}, answer("A-01")]
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    self.delivery.deliver(self.question, result)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.exporter.get_finished_spans(), ())

    def test_other_categories_are_not_silently_treated_as_chat(self):
        for function in ("Reproduction", "Idea"):
            with self.subTest(function=function), self.assertRaisesRegex(NotImplementedError, "正式交付尚未接入"):
                self.delivery.deliver({"case_id": "B-01", "function": function}, {})
        self.assertFalse(self.directory.exists())

    def test_retrieval_uses_same_atomic_writer_and_preserves_original_result(self):
        result = {"case_id": "B-99", "papers": [{"filename": "真实论文.pdf", "reason": "正文分析了城市绿地与温度。"}]}
        returned = self.delivery.deliver({"case_id": "B-99", "function": "Literature_Retrieval"}, result)
        self.assertIs(returned, result)
        self.assertEqual(json.loads((self.directory / "B-99.json").read_bytes()), result)
        with self.assertRaisesRegex(ValueError, "B 类答卷"):
            self.delivery.deliver({"case_id": "B-99", "function": "Literature_Retrieval"}, {**result, "metadata": {}})

    def survey_result(self):
        return {"case_id": "C-01", "path": f"data/expert/C-01-综述-{self.team}.md", "markdown": "# 正式综述\n\n正文与 JSON 相同。\n"}

    def test_survey_writes_both_files_and_returns_original_dictionary(self):
        result = self.survey_result()
        returned = self.delivery.deliver({"case_id": "C-01", "function": "Survey"}, result)
        self.assertIs(returned, result)
        self.assertEqual(json.loads((self.directory / "C-01.json").read_bytes()), result)
        self.assertEqual((self.root / result["path"]).read_text(encoding="utf-8"), result["markdown"])
        self.assertEqual([span.attributes["tool_name"] for span in self.exporter.get_finished_spans()], ["write_report_markdown", "write_result_json"])
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_survey_cannot_choose_another_teams_report_or_a_symlink(self):
        result = self.survey_result()
        with self.assertRaisesRegex(ValueError, "固定规则"):
            self.delivery.deliver({"case_id": "C-01", "function": "Survey"}, {**result, "path": "data/expert/C-01-综述-other.md"})
        outside = self.root / "outside"
        outside.mkdir()
        expert = self.root / "data/expert"
        expert.parent.mkdir(exist_ok=True)
        expert.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.delivery.deliver({"case_id": "C-01", "function": "Survey"}, result)
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_markdown_does_not_publish_json_and_removes_temporary_file(self):
        result = self.survey_result()
        with patch("src.delivery.result.os.replace", side_effect=PermissionError("报告不可写")):
            with self.assertRaises(PermissionError):
                self.delivery.deliver({"case_id": "C-01", "function": "Survey"}, result)
        self.assertFalse((self.directory / "C-01.json").exists())
        self.assertFalse((self.root / result["path"]).exists())
        self.assertFalse(list(self.root.rglob("*.tmp")))

    def test_survey_readback_failure_does_not_return_success(self):
        result = self.survey_result()
        original_read = Path.read_bytes
        report = self.root / result["path"]
        with patch.object(Path, "read_bytes", lambda path: b"changed" if path == report else original_read(path)):
            with self.assertRaisesRegex(ValueError, "Markdown 回读"):
                self.delivery.deliver({"case_id": "C-01", "function": "Survey"}, result)
        self.assertFalse((self.directory / "C-01.json").exists())

    def test_survey_invalid_encoding_is_rejected_before_any_report_is_written(self):
        result = {**self.survey_result(), "markdown": "\ud800"}
        with self.assertRaises(UnicodeEncodeError):
            self.delivery.deliver({"case_id": "C-01", "function": "Survey"}, result)
        self.assertFalse((self.root / "data/expert").exists())

    def test_invalid_utf8_is_rejected_before_writing(self):
        with self.assertRaises(UnicodeEncodeError):
            self.delivery.deliver(self.question, {**answer(), "answer": "\ud800"})
        self.assertFalse(self.directory.exists())

    def test_case_id_cannot_write_outside_team_directory(self):
        for case_id in ("../other-team/A-99", "/tmp/A-99", "assigned/A-99"):
            with self.subTest(case_id=case_id), self.assertRaises(ValueError):
                self.delivery.deliver({"case_id": case_id, "function": "Chat"}, answer(case_id))
        self.assertFalse(self.directory.exists())

    def test_team_name_cannot_escape_workspace(self):
        for team in ("..", "../other-team", "/tmp", ""):
            with self.subTest(team=team), self.assertRaises(ValueError):
                ResultDelivery(self.root, team)

    def test_directory_symlink_cannot_redirect_writes(self):
        outside = self.root / "other-team"
        outside.mkdir()
        self.directory.parent.mkdir()
        self.directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.delivery.deliver(self.question, answer())
        self.assertEqual(list(outside.iterdir()), [])

    def test_result_symlink_is_rejected_without_overwriting_target(self):
        self.directory.mkdir(parents=True)
        outside = self.root / "other-team-result.json"
        outside.write_text("保持原状", encoding="utf-8")
        (self.directory / "A-99.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            self.delivery.deliver(self.question, answer())
        self.assertEqual(outside.read_text(encoding="utf-8"), "保持原状")

    def test_replace_failure_preserves_previous_file_and_cleans_temporary(self):
        self.directory.mkdir(parents=True)
        target = self.directory / "A-99.json"
        previous = json.dumps({**answer(), "answer": "已有答卷"}, ensure_ascii=False).encode("utf-8")
        target.write_bytes(previous)
        with patch("src.delivery.result.os.replace", side_effect=PermissionError("写入失败")):
            with self.assertRaises(PermissionError):
                self.delivery.deliver(self.question, answer())
        self.assertEqual(target.read_bytes(), previous)
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_temporary_creation_failure_does_not_produce_result(self):
        with patch("src.delivery.result.tempfile.NamedTemporaryFile", side_effect=PermissionError("写入失败")):
            with self.assertRaises(PermissionError):
                self.delivery.deliver(self.question, answer())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_readback_mismatch_does_not_return_success(self):
        original_read = Path.read_bytes
        target = self.directory / "A-99.json"

        def mismatched_read(path):
            return b"{}" if path == target else original_read(path)

        with patch.object(Path, "read_bytes", mismatched_read):
            with self.assertRaisesRegex(ValueError, "回读内容"):
                self.delivery.deliver(self.question, answer())
        self.assertEqual(list(self.directory.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
