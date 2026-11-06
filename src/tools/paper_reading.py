"""B/C 共用的摘要定位；实际文件读取由 PDFTools 记录官方工具追踪。"""

import re

from src.tools.pdf import PDFTools

# 首轮最多检查六页；未定位完整摘要时返回实际读到的内容。
MAX_ABSTRACT_PAGES = 6

# 按行识别中英文摘要起始标记，忽略英文大小写，并允许“摘 要”等排版空白。
ABSTRACT_START = re.compile(r"(?im)^[ \t]*(?:abstract\b|summary\b|摘\s*要)\s*[:：.—-]?\s*")
# 以关键词或引言标题识别摘要结束位置，兼容标题前的章节编号。
ABSTRACT_END = re.compile(
    r"(?im)^[ \t]*(?:(?:key\s*words?|index terms)\b|关\s*键\s*词|"
    r"(?:\d+(?:\.\d+)*[\s.)、]*)?(?:introduction\b|引言|绪论))"
)

def read_abstract(tools: PDFTools, paper: dict, *, max_pages: int = MAX_ABSTRACT_PAGES) -> dict:
    document = tools.read_pdf_pages(paper["path"], [1])
    pages = document["pages"]
    last = min(max(1, max_pages), max(1, document["total_pages"]))
    if last >= 2:
        pages.extend(tools.read_pdf_pages(paper["path"], [2])["pages"])
    while True:
        joined = "\n".join(page["text"] for page in pages)
        start = ABSTRACT_START.search(joined)
        end = ABSTRACT_END.search(joined, start.end()) if start else None
        if end is not None or len(pages) >= last:
            break
        pages.extend(tools.read_pdf_pages(paper["path"], [len(pages) + 1])["pages"])
    status = "found" if end is not None else "incomplete" if start is not None else "not_found"
    front_matter = []
    if start is not None:
        # 仅截取实际原文，不调用模型补写摘要；保留跨页位置以供证据回查。
        first = start.end()
        stop = end.start() if end is not None else len(joined)
        offset = 0
        abstract_pages = []
        for page in pages:
            prefix = page["text"][:max(0, start.start() - offset)]
            if prefix.strip():
                # 摘要之外保留出版信息，时间条件必须能根据真实内容判断。
                front_matter.append({**page, "text": prefix})
            left = max(0, first - offset)
            right = min(len(page["text"]), stop - offset)
            if left < right:
                abstract_pages.append({**page, "text": page["text"][left:right]})
            offset += len(page["text"]) + 1
        if abstract_pages:
            pages = abstract_pages
        else:
            status = "not_found"
    return {
        **paper, "total_pages": document["total_pages"], "abstract_status": status,
        "front_matter": front_matter, "pages": pages,
    }

