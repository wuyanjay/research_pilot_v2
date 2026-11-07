"""报告文本清洗、参考文献切分和证据文本判断。"""

import json
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata


# 与 C/E 的“参考文献”标题兼容；真正的正式格式仍由各题型校验。
REFERENCE_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?"
    r"(?:(?:[0-9]+|[一二三四五六七八九十]+)[.．、)）]\s*)?"
    r"(?:参考文献|references|bibliography)(?:\*\*)?\s*[:：]?\s*$"
)

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.*?)\1(?!`)", re.DOTALL)
_MATH_RE = re.compile(
    r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|"
    r"(?<![\\$])\$(?![$\s])[^\n$]*?(?<![\\\s])\$(?![$0-9])|"
    r"\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]"
)


@dataclass(frozen=True)
class PaperExcerpt:
    """论文中的一段连续原文及其真实 PDF 页码。"""

    page: int
    text: str


@dataclass(frozen=True)
class PaperDigest:
    """提供给报告的指定论文证据。"""

    filename: str
    title: str
    excerpts: tuple[PaperExcerpt, ...]


def normalize_evidence_text(text: str) -> str:
    """去掉 PDF 中不可见的控制字符，不补写或改写原文。"""
    return "".join(
        char
        for char in text
        if char in "\n\r\t" or not unicodedata.category(char).startswith("C")
    ).strip()


def has_evidence_text(text: str, *, filename: str, title: str) -> bool:
    """拒绝空文本，以及只有文件名或标题、没有实际正文的“证据”。"""
    cleaned = normalize_evidence_text(text)
    if not any(char.isalnum() for char in cleaned):
        return False
    normalized = " ".join(cleaned.split()).casefold()
    metadata = {
        " ".join(normalize_evidence_text(value).split()).casefold()
        for value in (filename, Path(filename).stem)
    }
    if normalized in metadata:
        return False
    normalized_title = " ".join(normalize_evidence_text(title).split()).casefold()
    if normalized == normalized_title and not re.search(r"[.;!?。；！？][\"'”’）)]*$", cleaned):
        return False
    return True


def _unwrap_fence(text: str) -> str | None:
    """只解开“整段正文被一个完整代码围栏包住”的情况。"""
    lines = text.splitlines()
    if not lines:
        return None
    opening = re.fullmatch(r" {0,3}(`{3,}|~{3,})(?:[A-Za-z0-9_+-]*)[ \t]*", lines[0])
    if opening is None:
        return None
    marker = opening[1]
    closing = re.compile(" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + ",}[ \\t]*")
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if closing.fullmatch(line)),
        None,
    )
    if end is None:
        raise ValueError("报告包装的 Markdown 代码围栏未完整闭合，请重新生成完整正文")
    if end != len(lines) - 1:
        # 开头是正文中的代码示例，后面还有报告内容，不能当成外层包装。
        return None
    return "\n".join(lines[1:end]).strip()


def clean_markdown(text: str) -> str:
    """去掉完整的外层代码围栏或 JSON 包装，返回 Markdown 正文。"""
    if not isinstance(text, str):
        raise ValueError("模型回复必须包含字符串 Markdown 正文")
    text = text.strip()
    for _ in range(6):
        unwrapped = _unwrap_fence(text)
        if unwrapped is not None:
            text = unwrapped
            continue
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise ValueError(
                    "模型返回的 JSON 包装不完整或无效，请重新生成完整 Markdown 正文"
                ) from exc
            if not isinstance(data, dict) or "markdown" not in data:
                raise ValueError(
                    "模型返回了旧版结构化字段或缺少 markdown；请直接输出完整 Markdown 正文"
                )
            markdown = data["markdown"]
            if not isinstance(markdown, str) or not markdown.strip():
                raise ValueError("JSON 包装的 markdown 必须是非空字符串")
            text = markdown.strip()
            continue
        return text
    raise ValueError("报告包装嵌套过深，请直接输出完整 Markdown 正文")


def split_references(markdown: str) -> tuple[str, str]:
    """按第一个不在代码围栏中的参考文献标题切分正文和参考文献部分。"""
    offset = 0
    fence = ""
    fence_length = 0
    for line in markdown.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        marker = _FENCE_RE.match(content)
        if marker:
            if not fence:
                fence, fence_length = marker[1][0], len(marker[1])
            elif (
                marker[1][0] == fence
                and len(marker[1]) >= fence_length
                and not marker[2].strip()
            ):
                fence = ""
        elif not fence and REFERENCE_HEADING_RE.fullmatch(content):
            return markdown[:offset], markdown[offset:]
        offset += len(line)
    return markdown, ""


def report_body(markdown: str) -> str:
    """返回参考文献标题之前的原文；空格和换行保留，用于精确计长。"""
    return split_references(markdown)[0]


def strip_references(markdown: str) -> str:
    """返回去掉已有参考文献章节的正文，用于交给程序重新组装。"""
    return split_references(markdown)[0].rstrip()


def prose_only(body: str) -> str:
    """遮住代码块、行内代码和数学公式，避免把示例当成正文引用。"""
    lines = []
    fence_character = ""
    fence_length = 0
    for line in body.splitlines(keepends=True):
        match = _FENCE_RE.match(line.rstrip("\r\n"))
        if fence_character:
            if (
                match
                and match[1][0] == fence_character
                and len(match[1]) >= fence_length
                and not match[2].strip()
            ):
                fence_character = ""
            lines.append("\n")
        elif match:
            fence_character = match[1][0]
            fence_length = len(match[1])
            lines.append("\n")
        else:
            lines.append(line)
    prose = _INLINE_CODE_RE.sub(" ", "".join(lines))
    # 方括号数学向量/区间不是文献引用；这里只影响检查视图，不改正式正文。
    return _MATH_RE.sub(lambda match: "\n" * match[0].count("\n") + " ", prose)
