"""E 类证据选取：按章节和相关性选段，保留真实 PDF 页码。"""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import time

from src.handlers.report_text import (
    PaperDigest,
    PaperExcerpt,
    has_evidence_text,
    normalize_evidence_text,
)


CONTEXT_CHARS = 40_000
WINDOW_CHARS = 2_400

_KINDS = (
    ("references", r"references|bibliography|参考文献"),
    ("appendix", r"appendi(?:x|ces)|supplement(?:ary)?(?:\s+materials?)?|附录|补充材料"),
    (
        "method",
        r"(?:proposed\s+)?method(?:ology|s)?|(?:our\s+)?approach|"
        r"(?:proposed\s+)?(?:algorithm|framework)s?|implementation(?:\s+details)?|"
        r"problem\s+(?:formulation|definition)|方法|研究方法|算法|模型设计|实现细节|问题定义",
    ),
    (
        "experiment",
        r"experiments?(?:\s+(?:setup|settings?|evaluation))?|"
        r"experimental(?:\s+(?:setup|settings?|evaluation))?|"
        r"evaluation(?:\s+(?:setup|protocol))?|datasets?|benchmarks?|baselines?|metrics|"
        r"实验(?:设置|设计|方法|配置|评估)?|数据集|评价指标|评估设置|基线",
    ),
    (
        "results",
        r"results?(?:\s+and\s+discussion)?|ablation(?:\s+stud(?:y|ies))?|analysis|"
        r"实验结果|结果(?:与讨论|分析)?|消融(?:实验|分析)?",
    ),
    (
        "overview",
        r"abstract|introduction|background|related\s+work|conclusions?|discussion|"
        r"摘要|引言|背景|相关工作|结论|讨论",
    ),
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[一-鿿]+")


@dataclass
class Section:
    kind: str
    pieces: list[tuple[int, int, str]] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(text) for _, _, text in self.pieces)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("E 类材料读取超出本题预算")


def _kind(line: str) -> str | None:
    value = line.strip().strip("#").strip()
    if len(value) > 160:
        return None
    value = re.sub(r"^[一二三四五六七八九十]+[、.．]\s*", "", value)
    value = re.sub(
        r"^(?:(?:\d+(?:\.\d+)*|[A-Z](?:\.\d+)*|[IVX]+)[.、):：]?\s+|"
        r"第[一二三四五六七八九十\d]+[章节]\s*)",
        "",
        value,
    )
    for kind, pattern in _KINDS:
        if re.match(
            r"^(?:" + pattern + r")"
            r"(?:\s*[:：.、]\s*.*|\s+(?:and|of|for|on|with|in|details|setup|settings|evaluation|[A-Z])\b.*)?$",
            value,
            re.I,
        ):
            return kind
    return None


def sections(pages: list[str], *, deadline: float | None = None) -> list[Section]:
    """把按页文本切成带章节类型的连续片段。"""
    result: list[Section] = []
    current = Section("overview")
    for number, text in enumerate(pages, 1):
        _check_deadline(deadline)
        start = 0
        offset = 0
        for line in text.splitlines(keepends=True):
            kind = _kind(line)
            if kind is not None and kind != current.kind:
                if offset > start:
                    current.pieces.append((number, start, text[start:offset]))
                if current.pieces:
                    result.append(current)
                current = Section(kind)
                start = offset
            offset += len(line)
        if len(text) > start:
            current.pieces.append((number, start, text[start:]))
    if current.pieces:
        result.append(current)
    return result


def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _TOKEN_RE.findall(lowered)
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


def _relevance(text: str, terms: set[str]) -> float:
    tokens = _tokenize(text)
    if not tokens or not terms:
        return 0.0
    counts = Counter(tokens)
    hits = terms.intersection(counts)
    return len(hits) + sum(min(counts[term], 3) for term in hits) / (len(tokens) ** 0.5 + 1)


def fair_budgets(costs: list[int], budget: int) -> list[int]:
    """按论文/章节长度公平分配字符预算，短材料没用完的额度会再分给长材料。"""
    allocated = [0] * len(costs)
    active = [index for index, cost in enumerate(costs) if cost > 0]
    while active and budget > 0:
        share = max(1, budget // len(active))
        following = []
        for index in active:
            give = min(share, costs[index] - allocated[index], budget)
            allocated[index] += give
            budget -= give
            if allocated[index] < costs[index]:
                following.append(index)
        active = following
    return allocated


def _windows(section: Section) -> list[tuple[int, int, str]]:
    """优先按段落切窗，只有超长段落才继续拆。"""
    result = []
    for page, base, text in section.pieces:
        start = 0
        for match in re.finditer(r"\n\s*\n", text):
            end = match.end()
            if end - start >= WINDOW_CHARS // 2:
                for offset in range(start, end, WINDOW_CHARS // 2):
                    value = text[offset:min(offset + WINDOW_CHARS, end)]
                    result.append((page, base + offset, value))
                start = end
        for offset in range(start, len(text), WINDOW_CHARS // 2):
            result.append((page, base + offset, text[offset:offset + WINDOW_CHARS]))
    return result


def best_window(text: str, terms: set[str], limit: int) -> tuple[int, str]:
    if len(text) <= limit:
        return 0, text
    step = max(1, limit // 2)
    starts = list(range(0, len(text) - limit + 1, step)) + [len(text) - limit]
    offset = max(starts, key=lambda number: _relevance(text[number:number + limit], terms))
    return offset, text[offset:offset + limit]


def _take(
    section: Section,
    budget: int,
    terms: set[str],
    deadline: float | None,
) -> list[tuple[int, int, str]]:
    if budget <= 0:
        return []
    if section.chars <= budget:
        return list(section.pieces)
    windows = _windows(section)
    ranked = sorted(
        enumerate(windows),
        key=lambda item: (
            -_relevance(item[1][2], terms),
            0 if item[0] in (0, len(windows) - 1) else 1,
            item[0],
        ),
    )
    selected = []
    for _, (page, start, text) in ranked:
        _check_deadline(deadline)
        if budget <= 0:
            break
        if any(
            current_page == page
            and start < position + len(value)
            and start + len(text) > position
            for current_page, position, value in selected
        ):
            continue
        if len(text) > budget:
            offset, text = best_window(text, terms, budget)
            start += offset
        selected.append((page, start, text))
        budget -= len(text)
    return sorted(selected, key=lambda item: (item[0], item[1]))


def build_digests(
    tools,
    papers: list[dict],
    query: str,
    *,
    budget: int = CONTEXT_CHARS,
    deadline: float | None = None,
) -> list[PaperDigest]:
    """读取本题指定 PDF，按 E 类章节权重选取证据并保留页码。"""
    terms = set(_tokenize(query))
    documents = []
    for paper in papers:
        _check_deadline(deadline)
        document = tools.read_pdf_pages(paper["path"], None)
        pages = [
            normalize_evidence_text(page["text"])
            for page in document["pages"]
        ]
        title = tools.quick_meta(paper["path"]).get("title") or Path(paper["filename"]).stem
        readable = [
            text if has_evidence_text(text, filename=paper["filename"], title=title) else ""
            for text in pages
        ]
        if not any(readable):
            raise ValueError(f"论文正文不可读：{paper['filename']}")
        core = [
            section
            for section in sections(readable, deadline=deadline)
            if section.kind in ("overview", "method", "experiment", "results")
        ]
        documents.append((paper, title, core))

    quotas = fair_budgets(
        [sum(section.chars for section in core) for _, _, core in documents],
        budget,
    )
    digests = []
    for (paper, title, core), quota in zip(documents, quotas):
        groups = [
            [section for section in core if section.kind == "overview"],
            [section for section in core if section.kind == "method"],
            [section for section in core if section.kind in ("experiment", "results")],
        ]
        weights = [1, 2, 2]
        present = sum(weight for group, weight in zip(groups, weights) if group)
        allocations = [
            min(sum(section.chars for section in group), quota * weight // max(1, present))
            for group, weight in zip(groups, weights)
        ]
        extra = fair_budgets(
            [
                max(0, sum(section.chars for section in group) - allocation)
                for group, allocation in zip(groups, allocations)
            ],
            max(0, quota - sum(allocations)),
        )
        selected = []
        for group, allocation, added in zip(groups, allocations, extra):
            per_section = fair_budgets(
                [section.chars for section in group],
                allocation + added,
            )
            for section, limit in zip(group, per_section):
                selected.extend(_take(section, limit, terms, deadline))
        selected.sort(key=lambda item: (item[0], item[1]))
        excerpts = tuple(
            PaperExcerpt(page, text.strip())
            for page, _, text in selected
            if has_evidence_text(text, filename=paper["filename"], title=title)
        )
        if not excerpts:
            raise ValueError(f"论文未选出可用证据：{paper['filename']}")
        digests.append(PaperDigest(paper["filename"], title, excerpts))
    return digests
