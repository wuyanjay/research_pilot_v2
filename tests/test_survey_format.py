"""C 类格式接收与正式引用回归，不依赖真实模型或赛事服务。"""

import unittest

from src.handlers.survey_format import CitationCoverageError, assemble_markdown, parse_json_object


class SurveyJSONTests(unittest.TestCase):
    def test_complete_object_fence_and_unique_prose_wrapped_object(self):
        expected = {"papers": [{"paper_id": 1}]}
        for text in (
            '{"papers":[{"paper_id":1}]}',
            '```json\n{"papers":[{"paper_id":1}]}\n```',
            '~~~JSON\n{"papers":[{"paper_id":1}]}\n~~~',
            '以下是结果：\n{"papers":[{"paper_id":1}]}\n说明结束。',
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_json_object(text), expected)

    def test_multiple_objects_truncation_and_non_object_are_not_repaired(self):
        for text in (
            '{"papers":[]} {"papers":[]}',
            '结果：{"papers":[{"paper_id":1}]',
            '```json\n{"papers":[]}',
            '```json\n{"papers":[]}\n```\n额外文字',
            '[{"papers":[]}]',
            '结果：[{"papers":[]}]',
            '42',
            'null',
            '没有可用结果',
            '',
        ):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'JSON|对象'):
                parse_json_object(text)

    def test_invalid_outer_object_cannot_fall_back_to_an_inner_object(self):
        with self.assertRaises(ValueError):
            parse_json_object('说明：{"bad": {"papers":[]}')


class SurveyCitationTests(unittest.TestCase):
    def setUp(self):
        self.sources = [
            {"paper_id": number, "filename": f"指定论文{number}.pdf", "read_pages": [1, 2, 3]}
            for number in range(1, 6)
        ]
        self.requirements = {"min_chars": 800, "min_references": 5}
        self.padding = "实际研究条件与比较对象需要清楚说明，结论仅适用于提供的材料。" * 30

    def assemble(self, content):
        return assemble_markdown("# 测试综述\n\n" + self.padding + "\n\n" + content, self.sources, self.requirements)

    def body(self, result):
        return result.split("## 参考文献", 1)[0]

    def test_page_ranges_and_lists_keep_only_the_original_paper_number(self):
        result = self.assemble("相关发现[1, p.1-2]。另一发现[2, p.1,3]。\n\n其他材料[3,4,5]。")
        body = self.body(result)
        self.assertIn("相关发现[1]", body)
        self.assertIn("另一发现[2]", body)
        self.assertNotIn("p.", body)
        self.assertIn("[5] 指定论文5.pdf", result)

    def test_same_paragraph_repeated_marker_is_consolidated_at_the_last_position(self):
        result = self.assemble("先前发现[1]，后续比较[1]。\n\n其余来源[2,3,4,5]。")
        body = self.body(result)
        self.assertEqual(body.count("[1]"), 1)
        self.assertIn("先前发现，后续比较[1]", body)

    def test_different_paragraphs_lists_and_single_pages_are_not_merged(self):
        result = self.assemble("第一段[1]。\n\n第二段[1]。\n\n- 第一项[1]\n- 第二项[1]\n\n明确页码[2, p. 1]与[2, p. 3]。\n\n其他[3,4,5]。")
        body = self.body(result)
        self.assertEqual(body.count("[1]"), 4)
        self.assertIn("[2, p. 1]", body)
        self.assertIn("[2, p. 3]", body)

    def test_code_math_and_links_are_preserved_and_do_not_count_as_sources(self):
        examples = '\n```text\n[2, p.1-2][3]\n```\n`[4]`\n[5](https://example.invalid)\n$[2]$\n\\([3]\\)'
        with self.assertRaisesRegex(ValueError, "至少需要 5"):
            self.assemble("实际引用[1]。" + examples)
        result = self.assemble("实际引用[1,2,3,4,5]。" + examples)
        self.assertIn(examples, result)

    def test_unknown_sources_and_unread_single_pages_still_fail(self):
        for marker, message in (("[6, p.1-2]", "不在指定"), ("[1, p. 99]", "未实际读取")):
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, message):
                self.assemble("引用[1,2,3,4,5]。" + marker)

    def test_forty_markers_allowed_and_forty_one_still_fail_across_paragraphs(self):
        paragraphs = [f"结论[1,2,3,4,5]。" for _ in range(40)]
        result = self.assemble("\n\n".join(paragraphs))
        self.assertEqual(self.body(result).count("[1,2,3,4,5]"), 40)
        with self.assertRaisesRegex(ValueError, "最多 40"):
            self.assemble("\n\n".join(paragraphs + ["额外结论[1]。傍证。 "]))

    def test_same_paragraph_many_duplicates_can_be_normalized_below_limit(self):
        result = self.assemble("结论[1,2,3,4,5]。" * 45)
        self.assertEqual(self.body(result).count("[1,2,3,4,5]"), 1)

    def test_reference_section_is_rebuilt_and_code_heading_is_not_the_boundary(self):
        result = self.assemble("```markdown\n## 参考文献\n```\n\n实际引用[1,2,3,4,5]。\n\n## 参考文献\n[1] 猜测.pdf")
        self.assertNotIn("猜测.pdf", result)
        self.assertIn("[5] 指定论文5.pdf", result)

    def test_exact_character_limit_includes_two_reference_separator_newlines(self):
        prefix = "# 边界\n\n[1,2,3,4,5]\n"
        exact = prefix + "字" * (4_998 - len(prefix))
        result = assemble_markdown(exact, self.sources, self.requirements)
        self.assertEqual(len(self.body(result)), 5_000)
        with self.assertRaisesRegex(ValueError, "5001"):
            assemble_markdown(exact + "字", self.sources, self.requirements)

    def test_partial_coverage_error_carries_complete_candidate_but_zero_is_ordinary_failure(self):
        with self.assertRaises(CitationCoverageError) as raised:
            self.assemble("实际引用[1,2,3,4]。")
        error = raised.exception
        self.assertEqual(error.cited, [1, 2, 3, 4])
        self.assertEqual(error.missing, [5])
        self.assertEqual(error.min_references, 5)
        self.assertIn("## 参考文献", error.markdown)
        self.assertNotIn("[5] 指定论文5.pdf", error.markdown)
        with self.assertRaises(ValueError) as zero:
            self.assemble("没有引用但正文足够长。" + self.padding)
        self.assertNotIsInstance(zero.exception, CitationCoverageError)

    def test_other_validation_and_utf8_failures_are_never_coverage_errors(self):
        invalid_cases = [
            ("# 超长\n" + "字" * 5_100 + " [1]", ValueError),
            (self.padding + "\n\n" + "\n\n".join("[1,2,3,4]" for _ in range(41)), ValueError),
            (self.padding + " [1,2,3,6]", ValueError),
            (self.padding + " [1, p. 99] [2,3,4]", ValueError),
            ("# 编码异常\n" + "字" * 800 + "\ud800 [1]", UnicodeEncodeError),
        ]
        for text, error_type in invalid_cases:
            with self.subTest(error=error_type.__name__), self.assertRaises(error_type) as raised:
                self.assemble(text)
            self.assertNotIsInstance(raised.exception, CitationCoverageError)


if __name__ == "__main__":
    unittest.main()
