"""B 类文献检索的轻量预筛：快速索引、词法筛选和有界摘要读取。"""

import logging
import re
import time

from src.tools.paper_reading import read_abstract

logger = logging.getLogger(__name__)

# 词法预筛保留的候选数；论文数不超过该值时全部保留。
PREFILTER_LEXICAL_K = 80
# 标题初筛后进入摘要读取的候选数。
MAX_SCREEN_PAPERS = 40
# B 类摘要最多读取的页数，避免为了找摘要翻遍前六页。
MAX_ABSTRACT_PAGES = 2
# 标题初筛输入里首页片段的截断长度。
QUICK_SNIPPET_CHARS = 120


def build_index(tools, papers: list[dict], deadline: float, cache: dict | None = None) -> list[dict]:
    """为全部论文建立轻量索引；不 OCR，结果可按文件名列表跨题复用。"""
    key = tuple(paper["filename"] for paper in papers)
    if cache is not None and cache.get("key") == key:
        return cache["index"]

    index = []
    complete = True
    for paper in papers:
        if time.monotonic() >= deadline:
            complete = False
            index.append({
                "filename": paper["filename"],
                "path": paper["path"],
                "title": "",
                "snippet": "",
                "total_pages": 0,
            })
            continue
        meta = tools.quick_meta(paper["path"])
        index.append({
            "filename": paper["filename"],
            "path": paper["path"],
            "title": meta.get("title", ""),
            "snippet": meta.get("snippet", ""),
            "total_pages": meta.get("total_pages", 0),
        })
    if cache is not None and complete:
        cache["key"] = key
        cache["index"] = index
    return index


def _terms(criteria: dict, question: dict) -> list[str]:
    terms = []
    for word in criteria["search_terms"]["zh"] + criteria["search_terms"]["en"]:
        word = word.strip().lower()
        if len(word) >= 2 and word not in terms:
            terms.append(word)
    if terms:
        return terms
    # 判断依据没给出术语时，从标题和原题切出短片段兜底。
    text = (question.get("title", "") + " " + question.get("input", "")).strip()
    for word in re.split(r"[\s,，、;；。.!?！？:：()（）\[\]“”\"'《》<>/\\|]+", text):
        word = word.strip().lower()
        if len(word) >= 2 and word not in terms:
            terms.append(word)
    return terms[:12]


def lexical_prefilter(index: list[dict], criteria: dict, question: dict) -> list[dict]:
    """用中英文术语给文件名/标题/首页片段打分，保留前 PREFILTER_LEXICAL_K 篇。"""
    terms = _terms(criteria, question)
    scored = []
    for item in index:
        text = (item["filename"] + " " + item.get("title", "") + " " + item.get("snippet", "")).lower()
        score = sum(1 for term in terms if term in text)
        scored.append((score, item))
    scored.sort(key=lambda row: (-row[0], row[1]["filename"]))

    # 有命中的论文优先；不足 K 篇时再用零分论文补足，避免把明显无关的论文直接送进摘要。
    positive = [row for row in scored if row[0] > 0]
    chosen = positive[:PREFILTER_LEXICAL_K]
    if len(chosen) < PREFILTER_LEXICAL_K:
        remainder = [row for row in scored if row[0] == 0]
        chosen.extend(remainder[:PREFILTER_LEXICAL_K - len(chosen)])
    return [row[1] for row in chosen]


def read_summaries(tools, papers: list[dict], deadline: float,
                   max_pages: int = MAX_ABSTRACT_PAGES) -> list[dict]:
    """只对候选论文读取摘要；单篇失败时用首页片段兜底。"""
    summaries = []
    for paper in papers:
        if time.monotonic() >= deadline:
            summaries.append(_fallback_summary(paper))
            continue
        try:
            summaries.append(read_abstract(tools, paper, max_pages=max_pages))
        except Exception as exc:
            # 单篇损坏、加密或 OCR 失败不应终止整题；保留首页片段继续初筛。
            logger.warning("摘要读取失败：%s（%s）", paper["filename"], type(exc).__name__)
            summaries.append(_fallback_summary(paper))
    return summaries


def _fallback_summary(paper: dict) -> dict:
    snippet = paper.get("snippet", "")
    return {
        **paper,
        "abstract_status": "not_found",
        "front_matter": [],
        "pages": [{"page": 1, "text": snippet, "method": "text"}] if snippet else [],
    }
