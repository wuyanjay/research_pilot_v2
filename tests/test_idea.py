"""E 类离线测试：引用格式、内容边界、证据选取和 handler 编排。"""

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.handlers.idea import IdeaHandler
from src.handlers.idea_format import body_limit_issues, format_idea
from src.handlers.idea_review import idea_min_citations, review_idea_body
from src.handlers.report_text import PaperDigest, PaperExcerpt, clean_markdown


def digest():
    return PaperDigest(
        "urban.pdf",
        "Urban Heat Paper",
        (
            PaperExcerpt(1, "This paper studies urban heat and walking routes."),
            PaperExcerpt(2, "The method uses a routing baseline and measured metrics."),
        ),
    )


def question(text="阅读论文，提出研究方案。"):
    return {
        "case_id": "E-99",
        "function": "Idea",
        "title": "城市热暴露",
        "input": text,
        "materials": ["urban.pdf"],
        "constraints": ["将报告保存到 data/expert"],
        "expected_output": "返回 case_id、path 和 markdown",
        "output_schema_ref": "Idea",
    }


class IdeaFormatTests(unittest.TestCase):
    def test_format_idea_canonicalises_citations_and_builds_references(self):
        body = "# 报告\n\n## 研究问题\n\n引用 [1] 和 [1, p.2]。\n"
        markdown = format_idea(body, [digest()])
        self.assertIn("## 参考文献", markdown)
        self.assertIn("[1] Urban Heat Paper，urban.pdf", markdown)
        self.assertIn("[1, p. 2]", markdown)

    def test_invalid_number_page_and_missing_citation_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "引用编号 2"):
            format_idea("# 报告\n\n引用 [2]。\n", [digest()])
        with self.assertRaisesRegex(ValueError, "页码未出现在提供的摘录中"):
            format_idea("# 报告\n\n引用 [1, p. 9]。\n", [digest()])
        with self.assertRaisesRegex(ValueError, "缺少标准编号引用"):
            format_idea("# 报告\n\n没有引用。\n", [digest()])

    def test_body_limits_count_text_before_references(self):
        self.assertEqual(body_limit_issues("# 报告\n\n" + "字" * 900), [])
        self.assertTrue(any("最多" in issue for issue in body_limit_issues("# 报告\n\n" + "字" * 4600)))
        self.assertTrue(any("至少" in issue for issue in body_limit_issues("# 报告\n\n短")))

    def test_clean_markdown_unwraps_fence_and_json(self):
        self.assertEqual(clean_markdown("```markdown\n# 标题\n正文\n```"), "# 标题\n正文")
        self.assertEqual(clean_markdown('{"markdown":"# 标题\\n正文"}'), "# 标题\n正文")
        with self.assertRaisesRegex(ValueError, "未完整闭合"):
            clean_markdown("```markdown\n# 标题")


class IdeaReviewTests(unittest.TestCase):
    def test_minimum_citations_honour_explicit_task_quota(self):
        self.assertEqual(idea_min_citations({**question(), "expected_output": "至少引用 2 篇论文"}, 3), 2)
        self.assertEqual(idea_min_citations({**question(), "input": "引用全部论文"}, 3), 3)
        self.assertEqual(idea_min_citations(question(), 3), 1)

    def test_placeholder_is_a_hard_error(self):
        body = "# 报告\n\n## 研究问题与问题边界\n\n待补充\n"
        review = review_idea_body(body, question(), [digest()])
        self.assertTrue(any("占位" in error for error in review.errors))

    def test_simulated_observation_cannot_be_presented_as_paper_result(self):
        text = "题目给定的模拟观察显示：最短路线无遮荫路段较多，但没有测试替代策略。"
        body = "# 报告\n\n## 研究问题与问题边界\n\n论文实测表明该模拟观察就是论文结论。\n"
        review = review_idea_body(body, question(text), [digest()])
        self.assertTrue(any("不得将题目模拟观察" in error for error in review.errors))

    def test_missing_content_is_warning_not_hard_error(self):
        body = "# 报告\n\n## 研究问题与问题边界\n\n我们研究城市步行热暴露问题。\n"
        review = review_idea_body(body, question(), [digest()])
        self.assertEqual(review.errors, [])
        self.assertTrue(review.warnings)


class FakePDFTools:
    def __init__(self, materials, papers_directory, cache_directory=None, tessdata=None):
        self.materials = materials

    def list_papers(self):
        return [{"filename": "urban.pdf", "path": "/fake/urban.pdf"}]

    def quick_meta(self, path):
        return {"title": "Urban Heat Paper", "total_pages": 3}

    def read_pdf_pages(self, path, pages=None, ocr=True):
        return {
            "filename": "urban.pdf",
            "path": path,
            "total_pages": 3,
            "pages": [
                {"page": 1, "text": "Abstract\nThis paper studies urban heat and walking routes in summer. " * 8},
                {"page": 2, "text": "Method\nWe propose a routing baseline using shade coverage and walking distance. " * 8},
                {"page": 3, "text": "Experiments\nWe evaluate the method with metrics and a controlled dataset. " * 8},
            ],
        }


class FakeLLM:
    timeout_s = 900.0

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.reply


REPORT = """# 城市热暴露研究方案

## 研究问题与问题边界
在夏季高温条件下，如何减少步行者的热暴露，同时把绕行距离控制在可接受范围内？本方案只研究城市步行路线，不推广到骑行或机动车路线。[1]

## 研究动机、已有方法局限和模拟观察的证据边界
已有研究使用城市气候观测和步行规划方法分析热暴露。现有方法多关注单一最短路径，较少同时比较遮荫覆盖与绕行成本。题目给出的观察只是待分析、待验证现象，不能视为论文结论。[1]

## 核心 Idea 与技术路线
拟提出一种遮荫感知的路线规划机制。技术路线为：首先构建路段遮荫与热暴露特征；其次在候选路径上计算暴露成本和绕行成本；最后用多目标搜索选出推荐路线。该机制是拟议方案，效果尚待验证。

## 基线、数据集、实验设计、消融方案和量化指标
基线采用最短路径和最小暴露路径。数据来源包括实测遮荫覆盖、路网和气象条件，并按日期、时段和城区划分训练与测试数据。实验步骤为数据准备、路径生成、指标计算和显著性检验；每组实验重复多次并报告均值与方差。消融方案分别移除遮荫项、绕行项和热暴露项，以确认各组成部分的贡献。量化指标包括平均热暴露、绕行比例和到达时间，同时记录计算开销。

## 指定论文的互补关系
当前只有单篇指定论文，材料不足以作跨论文互补比较，因此不编造第二篇论文。[1]

## 可行性、实施成本、失败边界和风险
方案可行，实施成本主要是数据采集、路网预处理和路径搜索的计算开销，可在普通 CPU 环境完成原型验证。失败情形包括遮荫数据缺失、路网不连通、极端天气和传感器误差；适用边界为夏季白天城市步行场景，不适用于夜间或大范围区域。风险包括数据偏差、模型过拟合和绕行距离过大，需要通过外部验证和敏感性分析控制。

## 创新论证及可证伪方式
创新点是把遮荫覆盖与绕行成本联合建模，而不是只优化最短距离。可证伪条件是：如果联合策略相对基线不能降低热暴露，或绕行成本超过预设阈值，则核心假设被推翻。
"""


class IdeaHandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_handler_rejects_description_only_materials(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        handler = IdeaHandler(
            FakeLLM(REPORT),
            papers_directory=Path(temporary.name) / "papers",
            work_directory=Path(temporary.name) / "work",
            team="测试队伍",
        )
        with self.assertRaisesRegex(ValueError, "未提供显式 PDF"):
            asyncio.run(handler.handle({**question(), "materials": ["本题指定的论文 PDF"]}))

    def test_handler_rejects_impossible_citation_quota_before_model_call(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        llm = FakeLLM(REPORT)
        with patch("src.handlers.idea.PDFTools", FakePDFTools):
            handler = IdeaHandler(
                llm,
                papers_directory=Path(temporary.name) / "papers",
                work_directory=Path(temporary.name) / "work",
                team="测试队伍",
            )
            with self.assertRaisesRegex(ValueError, "至少引用 2 篇"):
                asyncio.run(handler.handle({**question(), "expected_output": "至少引用 2 篇论文"}))
        self.assertEqual(llm.calls, [])

    def test_handler_builds_report_and_returns_fixed_path(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name) / "work"
        llm = FakeLLM(REPORT)
        with patch("src.handlers.idea.PDFTools", FakePDFTools):
            handler = IdeaHandler(
                llm,
                papers_directory=Path(temporary.name) / "papers",
                work_directory=work,
                team="测试队伍",
            )
            result = asyncio.run(handler.handle(question()))
        self.assertEqual(result["case_id"], "E-99")
        self.assertEqual(result["path"], "data/expert/E-99-灵感Idea报告-测试队伍.md")
        self.assertIn("## 参考文献", result["markdown"])
        self.assertIn("Urban Heat Paper，urban.pdf", result["markdown"])
        self.assertEqual(len(llm.calls), 1)

    def test_handler_retries_invalid_report_until_attempts_exhausted(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name) / "work"
        llm = FakeLLM("不是 Markdown 报告")
        with patch("src.handlers.idea.PDFTools", FakePDFTools):
            handler = IdeaHandler(
                llm,
                papers_directory=Path(temporary.name) / "papers",
                work_directory=work,
                team="测试队伍",
            )
            with self.assertRaisesRegex(ValueError, "E 类报告生成失败"):
                asyncio.run(handler.handle(question()))
        self.assertEqual(len(llm.calls), 3)
        self.assertTrue(list((work / "E-99" / "idea").glob("attempt-*.json")))


if __name__ == "__main__":
    unittest.main()
