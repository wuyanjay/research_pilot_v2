"""定位当前题目的 PDF，按页提取正文并检索原文证据。"""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from agenthack import tool
import pymupdf

OCR_LANGUAGE = "eng+chi_sim"
OCR_DPI = 300
DEFAULT_SEARCH_LIMIT = 20
SNIPPET_CONTEXT_CHARS = 160


class PDFTools:
    def __init__(
        self,
        materials: list[str] | None,
        papers_directory: str | Path,
        cache_directory: str | Path | None = None,
        tessdata: str | Path | None = None,
    ):
        # 目录由调用方确定；工具不读取工作区、队名、派题或环境配置。
        # None 明确表示全库模式；空列表仍表示没有材料。
        self.materials = list(materials) if materials is not None else None
        self.papers_directory = Path(papers_directory).resolve()
        self.cache_directory = Path(cache_directory).resolve() if cache_directory is not None else None
        self.tessdata = str(Path(tessdata).resolve()) if tessdata is not None else None
        self._papers = None
        self._cache = {}

    def _resolve_papers(self) -> list[dict]:
        if self._papers is not None:
            return self._papers

        materials = self.materials
        if materials is None:
            # 全库模式只枚举论文目录直接包含的 PDF，不读取正文。
            materials = []
            for path in sorted(self.papers_directory.iterdir()):
                if path.is_file() and path.suffix.lower() == ".pdf":
                    materials.append(str(path))

        papers = []
        seen = set()
        for material in materials:
            requested = Path(material)
            if ".." in requested.parts:
                raise ValueError("论文材料路径不能包含 '..'")
            if requested.is_absolute():
                candidate = requested
            elif len(requested.parts) > 1 or material.startswith("./"):
                # 相对路径以调用方给定的论文目录为基准。
                candidate = self.papers_directory / requested
            else:
                # 仅枚举目录项定位指定文件，不打开其他论文；文件名不作为 glob 模式。
                matches = [
                    path for path in self.papers_directory.rglob("*")
                    if path.name == requested.name and path.is_file()
                ]
                if not matches:
                    raise FileNotFoundError(f"未找到指定论文：{material}")
                if len(matches) > 1:
                    raise ValueError(f"论文文件名存在重名，无法唯一定位：{material}")
                candidate = matches[0]

            path = candidate.resolve(strict=True)
            if not path.is_relative_to(self.papers_directory):
                raise PermissionError("论文路径超出调用方指定的论文目录")
            if not path.is_file() or path.suffix.lower() != ".pdf":
                raise ValueError(f"材料必须是 PDF 文件：{material}")
            if path not in seen:
                papers.append({"filename": path.name, "path": str(path)})
                seen.add(path)

        self._papers = papers
        return papers

    def _paper_path(self, absolute_path: str | Path) -> Path:
        requested = Path(absolute_path)
        if not requested.is_absolute():
            raise ValueError("读取和检索 PDF 必须使用绝对路径")
        path = requested.resolve(strict=True)
        allowed = {paper["path"] for paper in self._resolve_papers()}
        if str(path) not in allowed:
            raise PermissionError("论文不在本次允许访问的论文范围内")
        return path

    @tool(name="list_papers")
    def list_papers(self) -> list[dict]:
        """输入：无方法参数；materials 和 papers_directory 在创建对象时绑定。
        materials 为 None 时列出论文目录直接包含的全部 PDF；文件名列表仅定位指定论文；
        空列表表示没有材料。

        输出（JSON 数组，没有论文时为 []）：
        [
            {
                "filename": "<真实完整 PDF 文件名>",
                "path": "<该 PDF 的绝对路径>"
            }
        ]
        """
        return [dict(paper) for paper in self._resolve_papers()]

    def _page_cache(self, path: Path) -> tuple[dict, Path | None]:
        information = path.stat()
        fingerprint = {
            "path": str(path), "size": information.st_size, "mtime_ns": information.st_mtime_ns,
            "parser_version": pymupdf.VersionBind, "language": OCR_LANGUAGE, "tessdata": self.tessdata,
        }
        cached = self._cache.get(path)
        cache_path = None
        if self.cache_directory is not None:
            name = hashlib.sha256(str(path).encode("utf-8")).hexdigest() + ".json"
            cache_path = self.cache_directory / name
            if cached is None and cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached is None or cached["fingerprint"] != fingerprint:
            cached = {"fingerprint": fingerprint, "pages": {}}
        self._cache[path] = cached
        return cached, cache_path

    def _save_cache(self, cache_path: Path, cached: dict) -> None:
        # 缓存目录由业务侧显式传入，正式运行时应指向本队 work 子目录。
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=cache_path.parent,
                prefix=".pdf-cache-", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(cached, temporary, ensure_ascii=False)
            os.replace(temporary_path, cache_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _read_pages(self, path: Path, pages: list[int] | None) -> dict:
        cached, cache_path = self._page_cache(path)
        changed = False
        extracted = []
        with pymupdf.open(path) as document:
            if not document.is_pdf:
                raise ValueError("指定材料不是有效的 PDF 文档")
            if document.needs_pass:
                raise ValueError("PDF 需要密码，无法读取")
            total_pages = document.page_count
            selected = list(range(1, total_pages + 1)) if pages is None else pages
            for number in selected:
                if type(number) is not int or not 1 <= number <= total_pages:
                    raise ValueError(f"PDF 页码必须是 1 到 {total_pages} 之间的整数")

            for number in selected:
                key = str(number)
                if key not in cached["pages"]:
                    page = document.load_page(number - 1)
                    text = page.get_text(sort=True).strip()
                    method = "text"
                    if not text:
                        # 只有文字层未提取到内容时才 OCR；语言数据是预训练文件。
                        textpage = page.get_textpage_ocr(
                            language=OCR_LANGUAGE, dpi=OCR_DPI, full=True, tessdata=self.tessdata,
                        )
                        text = page.get_text(textpage=textpage, sort=True).strip()
                        method = "ocr"
                    cached["pages"][key] = {"page": number, "text": text, "method": method}
                    changed = True
                # 返回副本，避免调用方修改内部缓存。
                extracted.append(dict(cached["pages"][key]))

        if changed and cache_path is not None:
            self._save_cache(cache_path, cached)
        return {
            "filename": path.name, "path": str(path), "total_pages": total_pages, "pages": extracted,
        }

    @tool(name="read_pdf_pages")
    def read_pdf_pages(self, absolute_path: str | Path, pages: list[int] | None = None) -> dict:
        """输入：absolute_path 为本题 PDF 绝对路径；pages 为从 1 开始的页序号列表，
        省略或传入 None 时读取全文。例如：
        {
            "absolute_path": "/data/papers/<完整文件名>.pdf",
            "pages": [1, 2]
        }

        输出（JSON 对象，示例 PDF 共 10 页，本次读取第 1、2 页）：
        {
            "filename": "<真实完整 PDF 文件名>",
            "path": "<该 PDF 的绝对路径>",
            "total_pages": 10,
            "pages": [
                {
                    "page": 1,
                    "text": "<第 1 页的完整文字层正文>",
                    "method": "text"
                },
                {
                    "page": 2,
                    "text": "<第 2 页的完整 OCR 识别正文>",
                    "method": "ocr"
                }
            ]
        }
        """
        return self._read_pages(self._paper_path(absolute_path), pages)

    @tool(name="search_pdf_text")
    def search_pdf_text(
        self,
        absolute_paths: list[str | Path],
        keywords: list[str],
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[dict]:
        """输入：absolute_paths 为 PDF 绝对路径列表；keywords 为检索关键词列表；
        limit 为最多返回的命中记录数，默认 20，不限制搜索页数。例如：
        {
            "absolute_paths": ["/data/papers/<完整文件名>.pdf"],
            "keywords": ["<关键词 1>", "<关键词 2>"],
            "limit": 5
        }

        输出（JSON 数组，无命中时为 []）：
        [
            {
                "filename": "<命中论文的真实完整文件名>",
                "path": "<命中论文的绝对路径>",
                "page": 3,
                "snippet": "<命中上下文片段 1>[中间内容省略]<命中上下文片段 2>",
                "matched_keywords": ["<该页命中的关键词>"]
            }
        ]
        """
        terms = list(dict.fromkeys(word.strip() for word in keywords if word.strip()))
        if not terms:
            raise ValueError("检索关键词不能为空")
        if type(limit) is not int or limit <= 0:
            raise ValueError("检索返回数量必须是正整数")

        paths = list(dict.fromkeys(self._paper_path(path) for path in absolute_paths))
        hits = []
        for path in paths:
            # 复用内部读取，不重复记录嵌套工具；limit 不限制搜索页数。
            document = self._read_pages(path, None)
            for page in document["pages"]:
                intervals = []
                matched_keywords = []
                for word in terms:
                    # 前瞻也保留同一关键词相互重叠的出现位置。
                    matches = list(re.finditer("(?=" + re.escape(word) + ")", page["text"], re.IGNORECASE))
                    if matches:
                        matched_keywords.append(word)
                    for match in matches:
                        intervals.append((
                            max(0, match.start() - SNIPPET_CONTEXT_CHARS),
                            min(len(page["text"]), match.start() + len(word) + SNIPPET_CONTEXT_CHARS),
                        ))
                if not intervals:
                    continue
                merged = []
                for start, end in sorted(intervals):
                    if merged and start <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                    else:
                        merged.append((start, end))
                hits.append({
                    "filename": path.name, "path": str(path), "page": page["page"],
                    "snippet": "[中间内容省略]".join(page["text"][start:end] for start, end in merged),
                    "matched_keywords": matched_keywords,
                })
        # 仅按页内关键词覆盖数选择证据片段，不作为最终论文排序。
        hits.sort(key=lambda hit: (-len(hit["matched_keywords"]), hit["path"], hit["page"]))
        return hits[:limit]
