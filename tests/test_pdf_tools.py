"""在临时 PDF 与官方 SDK 会话中验证定位、分页、真实 OCR、缓存和检索。"""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agenthack.telemetry import Telemetry
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
import pymupdf

from src.tools import PDFTools


def write_pdf(path, texts):
    with pymupdf.open() as document:
        for text in texts:
            page = document.new_page()
            page.insert_text((50, 80), text, fontsize=18)
        document.save(path)


def write_scanned_pdf(path, mixed=False):
    """将可核对的中英文页面光栅化，生成没有文字层的扫描页。"""
    with pymupdf.open() as source:
        page = source.new_page(width=640, height=300)
        page.insert_text((50, 90), "Urban green space", fontsize=30)
        page.insert_text((50, 160), "城市绿地", fontsize=32, fontname="china-s")
        image = page.get_pixmap(dpi=200).tobytes("png")
    with pymupdf.open() as document:
        if mixed:
            page = document.new_page(width=640, height=300)
            page.insert_text((50, 90), "Native text page", fontsize=30)
        page = document.new_page(width=640, height=300)
        page.insert_image(page.rect, stream=image)
        document.save(path)


class PDFToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.papers = self.root / "papers"
        self.papers.mkdir()
        self.cache = self.root / "work/pdf_cache"
        self.paper = self.papers / "城市 [绿地] 研究.pdf"
        write_pdf(self.paper, [
            "Urban vegetation and surface temperature.",
            "Study design and control variables.",
            "SATELLITE data show green space and cooling.",
        ])
        self.tools = PDFTools([self.paper.name], self.papers, self.cache)
        self.exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.addCleanup(provider.shutdown)
        session = Telemetry(provider=provider).session(task_id="pdf-tools-test", task_type="Literature_Retrieval")
        session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)

    def test_lists_absolute_relative_and_exact_filename_without_reading_pdf(self):
        nested = self.papers / "nested"
        nested.mkdir()
        second = nested / "第二篇.pdf"
        write_pdf(second, ["Second paper"])
        unrelated = self.papers / "unrelated.pdf"
        write_pdf(unrelated, ["Not allowed"])
        materials = [str(self.paper), self.paper.name, "./" + self.paper.name, "nested/第二篇.pdf"]
        original = deepcopy(materials)
        tools = PDFTools(materials, self.papers)
        with patch("src.tools.pdf.pymupdf.open", side_effect=AssertionError("定位时不能读取正文")):
            actual = tools.list_papers()
        self.assertEqual(actual, [
            {"filename": self.paper.name, "path": str(self.paper)},
            {"filename": second.name, "path": str(second)},
        ])
        self.assertEqual(materials, original)
        actual[0]["path"] = "changed"
        self.assertEqual(tools.list_papers()[0]["path"], str(self.paper))
        self.assertFalse(self.cache.exists())

    def test_full_library_lists_direct_pdf_files_in_stable_order_without_reading(self):
        second = self.papers / "another.PDF"
        write_pdf(second, ["Another paper"])
        (self.papers / "notes.txt").write_text("Not a PDF", encoding="utf-8")
        nested = self.papers / "nested"
        nested.mkdir()
        write_pdf(nested / "nested.pdf", ["Nested paper"])
        tools = PDFTools(None, self.papers)
        with patch("src.tools.pdf.pymupdf.open", side_effect=AssertionError("定位时不能读取正文")):
            actual = tools.list_papers()
        self.assertEqual(actual, [
            {"filename": path.name, "path": str(path)}
            for path in sorted([self.paper, second])
        ])

    def test_full_library_paths_can_be_read_and_searched(self):
        second = self.papers / "second.pdf"
        write_pdf(second, ["Urban cooling in another study"])
        tools = PDFTools(None, self.papers, self.cache)
        papers = tools.list_papers()
        for paper in papers:
            result = tools.read_pdf_pages(paper["path"], [1])
            self.assertEqual(result["filename"], paper["filename"])
            self.assertTrue(result["pages"][0]["text"])
        hits = tools.search_pdf_text([paper["path"] for paper in papers], ["urban"])
        self.assertEqual({hit["path"] for hit in hits}, {str(self.paper), str(second)})

    def test_empty_materials_do_not_enable_full_library_mode(self):
        tools = PDFTools([], self.papers)
        self.assertEqual(tools.list_papers(), [])
        with patch("src.tools.pdf.pymupdf.open", side_effect=AssertionError("不能读取未授权正文")):
            with self.assertRaises(PermissionError):
                tools.read_pdf_pages(self.paper)

    def test_missing_ambiguous_outside_and_symlink_materials_fail(self):
        outside = self.root / "outside.pdf"
        write_pdf(outside, ["Outside"])
        for materials, error in [
            (["missing.pdf"], FileNotFoundError),
            (["../outside.pdf"], ValueError),
            ([str(outside)], PermissionError),
        ]:
            with self.subTest(materials=materials), self.assertRaises(error):
                PDFTools(materials, self.papers).list_papers()
        duplicate = self.papers / "nested"
        duplicate.mkdir()
        write_pdf(duplicate / self.paper.name, ["Duplicate"])
        with self.assertRaisesRegex(ValueError, "重名"):
            self.tools.list_papers()
        link = self.papers / "link.pdf"
        link.symlink_to(outside)
        for materials in (["link.pdf"], None):
            with self.subTest(materials=materials), self.assertRaises(PermissionError):
                PDFTools(materials, self.papers).list_papers()

    def test_read_and_search_require_authorized_absolute_paths(self):
        unrelated = self.papers / "unrelated.pdf"
        write_pdf(unrelated, ["Not allowed"])
        for call in (
            lambda: self.tools.read_pdf_pages(self.paper.name),
            lambda: self.tools.search_pdf_text([self.paper.name], ["urban"]),
        ):
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                call()
        with patch("src.tools.pdf.pymupdf.open", side_effect=AssertionError("不能读取未授权正文")):
            with self.assertRaises(PermissionError):
                self.tools.read_pdf_pages(unrelated)
            with self.assertRaises(PermissionError):
                self.tools.search_pdf_text([unrelated], ["urban"])

    def test_native_text_and_one_based_selected_pages_skip_ocr(self):
        with patch.object(pymupdf.Page, "get_textpage_ocr", side_effect=AssertionError("文字页不应 OCR")):
            result = self.tools.read_pdf_pages(self.paper, [3, 1])
        self.assertEqual(result["filename"], self.paper.name)
        self.assertEqual(result["path"], str(self.paper))
        self.assertEqual(result["total_pages"], 3)
        self.assertEqual([page["page"] for page in result["pages"]], [3, 1])
        self.assertTrue(all(page["method"] == "text" for page in result["pages"]))
        self.assertIn("SATELLITE", result["pages"][0]["text"])
        for pages in ([0], [4], [-1], [True], [1.5]):
            with self.subTest(pages=pages), self.assertRaisesRegex(ValueError, "页码"):
                self.tools.read_pdf_pages(self.paper, pages)

    def test_real_chinese_english_ocr_mixed_pages_and_disk_cache(self):
        scan = self.papers / "中英文扫描.pdf"
        write_scanned_pdf(scan, mixed=True)
        tools = PDFTools([scan.name], self.papers, self.cache)
        original = pymupdf.Page.get_textpage_ocr
        with patch.object(pymupdf.Page, "get_textpage_ocr", autospec=True, side_effect=original) as ocr:
            result = tools.read_pdf_pages(scan)
            self.assertEqual([page["method"] for page in result["pages"]], ["text", "ocr"])
            recognized = result["pages"][1]["text"]
            self.assertIn("urban", recognized.lower())
            self.assertIn("green", recognized.lower())
            self.assertIn("城市绿地", "".join(recognized.split()))
            self.assertEqual(tools.read_pdf_pages(scan), result)
            # 新对象读取磁盘缓存，也无需再次识别扫描页。
            fresh = PDFTools([scan.name], self.papers, self.cache)
            self.assertEqual(fresh.read_pdf_pages(scan), result)
            self.assertEqual(ocr.call_count, 1)
            self.assertEqual(ocr.call_args.kwargs["language"], "eng+chi_sim")
        result["pages"][1]["text"] = "changed"
        self.assertNotEqual(tools.read_pdf_pages(scan)["pages"][1]["text"], "changed")
        self.assertEqual(len(list(self.cache.glob("*.json"))), 1)
        self.assertFalse(list(self.cache.glob("*.tmp")))

    def test_file_change_invalidates_memory_and_disk_cache(self):
        original = self.tools.read_pdf_pages(self.paper)
        previous = self.paper.stat()
        self.paper.unlink()
        write_pdf(self.paper, ["Replacement paper with new content"])
        os.utime(self.paper, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000))
        changed = self.tools.read_pdf_pages(self.paper)
        self.assertNotEqual(changed, original)
        self.assertEqual(changed["total_pages"], 1)
        self.assertIn("Replacement", changed["pages"][0]["text"])
        fresh = PDFTools([self.paper.name], self.papers, self.cache)
        self.assertEqual(fresh.read_pdf_pages(self.paper), changed)

    def test_search_reads_later_pages_preserves_evidence_and_does_not_expand_scope(self):
        result = self.tools.search_pdf_text([self.paper, self.paper], ["satellite", "cooling"], limit=1)
        self.assertEqual(len(result), 1)
        hit = result[0]
        self.assertEqual(hit["page"], 3)
        self.assertEqual(hit["filename"], self.paper.name)
        self.assertEqual(hit["path"], str(self.paper))
        self.assertEqual(hit["matched_keywords"], ["satellite", "cooling"])
        self.assertIn("SATELLITE", hit["snippet"])
        self.assertIn(hit["snippet"], self.tools.read_pdf_pages(self.paper, [3])["pages"][0]["text"])
        self.assertEqual(self.tools.search_pdf_text([self.paper], ["absent-keyword"]), [])
        for keywords, limit in [([], 1), ([" "], 1), (["urban"], 0)]:
            with self.subTest(keywords=keywords, limit=limit), self.assertRaises(ValueError):
                self.tools.search_pdf_text([self.paper], keywords, limit)

    def test_ocr_and_pdf_failures_propagate_without_empty_success(self):
        scan = self.papers / "scan.pdf"
        write_scanned_pdf(scan)
        tools = PDFTools([scan.name], self.papers, self.cache)
        error = RuntimeError("OCR 语言资源不可用")
        with patch.object(pymupdf.Page, "get_textpage_ocr", side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                tools.search_pdf_text([scan], ["urban"])
            self.assertIs(raised.exception, error)
        self.assertFalse(self.cache.exists())
        broken = self.papers / "broken.pdf"
        broken.write_bytes(b"not a PDF")
        with self.assertRaises(pymupdf.FileDataError):
            PDFTools([broken.name], self.papers).read_pdf_pages(broken)
        encrypted = self.papers / "encrypted.pdf"
        with pymupdf.open(self.paper) as document:
            document.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
        with self.assertRaisesRegex(ValueError, "密码"):
            PDFTools([encrypted.name], self.papers).read_pdf_pages(encrypted)

    def test_search_includes_every_occurrence_merges_contexts_and_preserves_order(self):
        text = "LST" + "a" * 500 + "vegetation" + "b" * 500 + "lst"
        document = {"filename": self.paper.name, "path": str(self.paper), "total_pages": 1,
                    "pages": [{"page": 1, "text": text, "method": "text"}]}
        with patch.object(self.tools, "_read_pages", return_value=document):
            hits = self.tools.search_pdf_text([self.paper], ["vegetation", "LST"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["matched_keywords"], ["vegetation", "LST"])
        self.assertEqual(hits[0]["snippet"], "[中间内容省略]".join([
            text[:163], text[343:673], text[853:],
        ]))
        text = "x" * 200 + "banana LST vegetation" + "y" * 200
        document["pages"][0]["text"] = text
        with patch.object(self.tools, "_read_pages", return_value=document):
            hits = self.tools.search_pdf_text([self.paper], ["ana", "LST", "vegetation"])
        self.assertEqual(hits[0]["snippet"], text[41:381])
        self.assertNotIn("[中间内容省略]", hits[0]["snippet"])

    def test_search_rank_counts_distinct_keywords_not_occurrence_count(self):
        document = {"pages": [
            {"page": 1, "text": "urban " * 100},
            {"page": 2, "text": "Urban and LST"},
            {"page": 3, "text": "urban and LST"},
        ]}
        with patch.object(self.tools, "_read_pages", return_value=document):
            hits = self.tools.search_pdf_text([self.paper], ["urban", "LST"], limit=2)
        self.assertEqual([hit["page"] for hit in hits], [2, 3])

    def test_each_public_operation_records_one_official_tool_span(self):
        self.tools.list_papers()
        self.tools.read_pdf_pages(self.paper, [1])
        self.tools.search_pdf_text([self.paper], ["urban"])
        spans = self.exporter.get_finished_spans()
        self.assertEqual([span.name for span in spans], ["agent.tool"] * 3)
        self.assertEqual([span.attributes["tool_name"] for span in spans], [
            "list_papers", "read_pdf_pages", "search_pdf_text",
        ])


if __name__ == "__main__":
    unittest.main()
