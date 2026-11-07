"""E 类引文解析、规范化和参考文献组装。"""

from dataclasses import dataclass
import re
import unicodedata

from src.handlers.report_text import (
    PaperDigest,
    has_evidence_text,
    prose_only,
    report_body,
    strip_references,
)


MIN_IDEA_BODY_CHARS = 800
MAX_IDEA_BODY_CHARS = 4500

_BRACKETS_RE = re.compile(r"(?<!\\)\[([^\[\]\n]*)\](?!\s*\()")
_CANDIDATE_RE = re.compile(r"^\s*[+-]?[0-9]")
_CITATION_RE = re.compile(
    r"^\s*(?P<numbers>[0-9]+(?:\s*[,，;；]\s*[0-9]+)*)\s*"
    r"(?:[,，]\s*pp?\.?\s*(?P<start>[0-9]+)\s*"
    r"(?:[-–—−]\s*(?P<end>[0-9]+))?)?\s*$",
    re.IGNORECASE,
)
_PAGE_LIST_RE = re.compile(
    r"^\s*(?P<number>[0-9]+)\s*[,，]\s*"
    r"(?P<pages>pp?\.?\s*[0-9]+(?:\s*[,，]\s*pp?\.?\s*[0-9]+)+)\s*$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"[0-9]+")

_BRACKET = re.compile(
    r"(?<!\\)\\?\[([^\[\]\n]+)\](?:\((?:[^()\n]|\([^()\n]*\))*\))?|"
    r"【([^【】\n]+)】|［([^［］\n]+)］"
)
_RANGE = re.compile(r"([1-9][0-9]{0,2})\s*[-–—]\s*([1-9][0-9]{0,2})")
_STANDARD = re.compile(r"\[([1-9][0-9]*), p\. ([1-9][0-9]*)\]")
_REFERENCE = re.compile(r"^\[([1-9][0-9]*)\] (.+)$")


class IdeaFormatError(ValueError):
    """正文引用或参考文献不能按赛事格式组装。"""


@dataclass(frozen=True)
class Citation:
    """一个合法引用标记；页码范围是否真实存在由 validate_citations 判断。"""

    numbers: tuple[int, ...]
    page_start: int | None = None
    page_end: int | None = None
    source: str = ""


def _small_integer(value: str) -> int | None:
    significant = value.lstrip("0") or "0"
    return int(significant) if len(significant) <= 9 else None


def parse_citations(body: str) -> tuple[list[Citation], list[str]]:
    """解析正文中的数字方括号引用，并报告格式或页码错误。"""
    citations: list[Citation] = []
    issues: list[str] = []
    for bracket in _BRACKETS_RE.finditer(prose_only(body)):
        content = bracket[1]
        if not _CANDIDATE_RE.match(content):
            continue
        source = bracket[0]
        display = source if len(source) <= 100 else source[:97] + "..."
        match = _CITATION_RE.fullmatch(content)
        if not match:
            page_list = _PAGE_LIST_RE.fullmatch(content)
            if page_list:
                number = _small_integer(page_list.group("number"))
                pages = [
                    _small_integer(value)
                    for value in _NUMBER_RE.findall(page_list.group("pages"))
                ]
                if number is None or any(page is None for page in pages):
                    issues.append(f"引用编号或页码过大：{display}")
                else:
                    citations.extend(
                        Citation((number,), page, page, source) for page in pages
                    )
                continue
            issues.append(f"引用格式无效：{display}")
            continue
        values = [_small_integer(value) for value in _NUMBER_RE.findall(match.group("numbers"))]
        start = _small_integer(match.group("start")) if match.group("start") is not None else None
        end = _small_integer(match.group("end")) if match.group("end") is not None else start
        if (
            any(value is None for value in values)
            or (match.group("start") is not None and start is None)
            or (match.group("end") is not None and end is None)
        ):
            issues.append(f"引用编号或页码过大：{display}")
            continue
        citations.append(
            Citation(tuple(value for value in values if value is not None), start, end, source)
        )
    return citations, issues


def cited_numbers(body: str, max_index: int) -> list[int]:
    """返回正文中实际出现且编号在指定论文范围内的论文号。"""
    citations, _ = parse_citations(body)
    return sorted(
        {
            number
            for citation in citations
            for number in citation.numbers
            if 1 <= number <= max_index
        }
    )


def citation_marker_count(body: str) -> int:
    """统计引用标记组数，复合引用 [1,2] 只算一组。"""
    return sum(
        1 for bracket in _BRACKETS_RE.finditer(prose_only(body))
        if _CANDIDATE_RE.match(bracket[1])
    )


def validate_citations(body: str, digests: list[PaperDigest]) -> tuple[list[int], list[str]]:
    """检查每个引用编号和页码都来自实际提供的论文摘录。"""
    citations, issues = parse_citations(body)
    cited: set[int] = set()
    invalid_numbers: set[int] = set()
    for citation in citations:
        for number in citation.numbers:
            if not 1 <= number <= len(digests):
                invalid_numbers.add(number)
                continue
            cited.add(number)
            if citation.page_start is None:
                continue
            start, end = citation.page_start, citation.page_end
            if end is None:
                continue
            digest = digests[number - 1]
            available = {
                excerpt.page
                for excerpt in digest.excerpts
                if excerpt.page > 0
                and has_evidence_text(
                    excerpt.text, filename=digest.filename, title=digest.title
                )
            }
            if start < 1 or end < start:
                issues.append(f"引用页码范围无效：{citation.source}")
            elif sum(start <= page <= end for page in available) != end - start + 1:
                issues.append(
                    f"引用页码没有指定论文摘录支持：{citation.source}，"
                    f"论文 {number} 可用 PDF 页码为 {sorted(available)}"
                )
    if invalid_numbers:
        issues.append(f"存在无效引用编号：{sorted(invalid_numbers)}")
    return sorted(cited), list(dict.fromkeys(issues))


def _prose_mask(text: str) -> str:
    """用等长空格遮住代码和数学公式，保证引用替换位置不偏移。"""
    def blank(value: str) -> str:
        return re.sub(r"[^\r\n]", " ", value)

    lines = []
    fence = ""
    fence_length = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            if (
                match
                and match[1][0] == fence
                and len(match[1]) >= fence_length
                and not match[2].strip()
            ):
                fence = ""
            lines.append(blank(line))
        elif match:
            fence, fence_length = match[1][0], len(match[1])
            lines.append(blank(line))
        else:
            lines.append(line)
    text = "".join(lines)
    text = re.sub(
        r"(?<!`)(`+)(?!`)(.*?)\1(?!`)",
        lambda match: blank(match[0]),
        text,
        flags=re.S,
    )
    return re.sub(
        r"(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|"
        r"(?<![\\$])\$(?![$\s])[^\n$]*?(?<![\\\s])\$(?![$0-9])|"
        r"\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]",
        lambda match: blank(match[0]),
        text,
    )


def _normalise_marker(token: str) -> str:
    value = unicodedata.normalize("NFKC", token).strip()
    value = value.replace("，", ",").replace("；", ";").replace("、", ",")
    # 兼容模型常见的 p.3 / pp.3 写法，统一成分析器能识别的 p. 3。
    value = re.sub(r"(?i)\b(pp?)\.\s*", lambda match: match.group(1) + ". ", value)
    match = _RANGE.fullmatch(value)
    if match:
        start, end = map(int, match.groups())
        if start > end or end - start > 149:
            raise IdeaFormatError("引用编号范围无效，请使用逐篇编号")
        value = ",".join(str(number) for number in range(start, end + 1))
    return "[" + value + "]"


def format_idea(
    markdown: str,
    digests: list[PaperDigest],
    *,
    min_citations: int = 1,
    require_pages: bool = False,
) -> str:
    """规范化正文引用并重建参考文献；不补造论文或页码。"""
    body = strip_references(markdown).strip()
    visible = _prose_mask(body)
    numbered: dict[int, int] = {}
    replacements: list[tuple[int, int, str]] = []

    for match in _BRACKET.finditer(visible):
        token = next(group for group in match.groups() if group is not None)
        normal = unicodedata.normalize("NFKC", token).strip()
        if not re.match(r"\^?[+\-]?[0-9]", normal):
            continue
        if (
            (match.start() and visible[match.start() - 1] == "[")
            or visible[match.end():match.end() + 1] == "]"
        ):
            raise IdeaFormatError("正文引用不能使用嵌套方括号")
        marker = _normalise_marker(token)
        citations, issues = parse_citations(marker)
        if issues or not citations:
            raise IdeaFormatError("引用格式无法识别，只能使用 [1] 或 [1, p. 3] 这种格式")
        output = []
        for citation in citations:
            for original in citation.numbers:
                if not 1 <= original <= len(digests):
                    raise IdeaFormatError(f"引用编号 {original} 不在指定论文目录中")
                if citation.page_start is None:
                    if not require_pages:
                        numbered.setdefault(original, original)
                        output.append(f"[{original}]")
                        continue
                    raise IdeaFormatError("正文引用缺少 PDF 页码，只能使用 [1, p. 3] 这种格式")
                start, end = citation.page_start, citation.page_end
                if end is None:
                    raise IdeaFormatError("引用页码范围无效")
                digest = digests[original - 1]
                available = {
                    excerpt.page
                    for excerpt in digest.excerpts
                    if excerpt.page > 0
                    and has_evidence_text(
                        excerpt.text, filename=digest.filename, title=digest.title
                    )
                }
                if start < 1 or end < start or end - start >= len(available):
                    raise IdeaFormatError("引用页码范围没有指定论文摘录支持")
                pages = list(range(start, end + 1))
                if not set(pages) <= available:
                    raise IdeaFormatError(f"论文 {original} 的引用页码未出现在提供的摘录中")
                number = numbered.setdefault(original, original)
                output.extend(f"[{number}, p. {page}]" for page in pages)
        replacements.append((match.start(), match.end(), "".join(dict.fromkeys(output))))

    if not numbered:
        raise IdeaFormatError("正文缺少标准编号引用，请引用实际提供的论文")
    if len(numbered) < min_citations:
        raise IdeaFormatError(
            f"题目要求至少引用 {min_citations} 篇实际指定论文，目前只有 {len(numbered)} 篇"
        )

    for start, end, value in reversed(replacements):
        body = body[:start] + value + body[end:]

    entries = []
    for original, number in sorted(numbered.items()):
        digest = digests[original - 1]
        title = " ".join(digest.title.split())
        if not title or not digest.filename or "\n" in digest.filename or "\r" in digest.filename:
            raise IdeaFormatError("指定论文标题或文件名缺失，无法生成参考文献")
        entries.append(f"[{number}] {title}，{digest.filename}")

    result = body + "\n\n## 参考文献\n\n" + "\n".join(entries) + "\n"
    validate_idea_format(result, require_pages=require_pages)
    return result


def validate_idea_format(markdown: str, *, require_pages: bool = False) -> None:
    """核对正文引用与参考文献一一对应，且格式符合正式提交要求。"""
    body = strip_references(markdown)
    tail = markdown[len(body):].strip()
    lines = tail.splitlines()
    if not lines or lines[0] != "## 参考文献":
        raise IdeaFormatError("参考文献标题必须为 ## 参考文献")
    references = []
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _REFERENCE.fullmatch(line)
        if not match:
            raise IdeaFormatError("参考文献必须逐行使用 [1] 条目格式")
        references.append(int(match[1]))
    if references != sorted(set(references)):
        raise IdeaFormatError("参考文献编号必须按指定论文编号升序且不重复")

    prose = prose_only(body)
    used = []
    for match in _BRACKET.finditer(prose):
        token = next(group for group in match.groups() if group is not None)
        if not re.match(
            r"\^?[+\-]?[0-9]", unicodedata.normalize("NFKC", token).strip()
        ):
            continue
        if (
            (match.start() and prose[match.start() - 1] == "[")
            or prose[match.end():match.end() + 1] == "]"
        ):
            raise IdeaFormatError("正文引用不能使用嵌套方括号")
        standard = _STANDARD.fullmatch(match[0])
        if not require_pages and standard is None:
            standard = re.fullmatch(r"\[([1-9][0-9]*)\]", match[0])
        if not standard:
            raise IdeaFormatError("正文引用只能为 [1] 或 [1, p. 3] 这种格式")
        used.append(int(standard[1]))
    if not used or set(used) != set(references):
        raise IdeaFormatError("正文引用与参考文献必须一一对应")


def body_limit_issues(markdown: str) -> list[str]:
    """检查 E 类正文长度；参考文献章节不计入上限。"""
    body = report_body(markdown)
    issues = []
    if len(body.strip()) < MIN_IDEA_BODY_CHARS:
        issues.append(f"正文至少需要 {MIN_IDEA_BODY_CHARS} 个字符")
    if len(body) > MAX_IDEA_BODY_CHARS:
        issues.append(
            f"正文为 {len(body)} 字符，最多 {MAX_IDEA_BODY_CHARS} 字符（参考文献不计入）"
        )
    return issues
