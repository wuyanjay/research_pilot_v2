"""C 类模型 JSON 接收、引用规范化及正式 Markdown 组装。"""

import json
import re


# 赛事正文限制；书目本身不计入正文，书目前的分隔换行计入。
MAX_BODY_CHARS = 5_000
MAX_CITATION_MARKERS = 40


class CitationCoverageError(ValueError):
    """正文合规但引用篇数不足时，携带可交付候选稿。"""

    def __init__(self, markdown: str, cited: list[int], missing: list[int], min_references: int):
        super().__init__(f"正文实际引用 {len(cited)} 篇，至少需要 {min_references} 篇指定论文")
        self.markdown = markdown
        self.cited = cited
        self.missing = missing
        self.min_references = min_references


def parse_json_object(text: str) -> dict:
    """接受完整对象及可确定的外围格式，不拼接截断内容或多个对象。"""
    raw = text.strip()
    if not raw:
        raise ValueError("模型回复为空，须返回完整 JSON 对象")

    # 围栏必须完整闭合；不从未闭合的代码块里猜测一份成功结果。
    if raw.startswith(("```", "~~~")):
        fenced = re.fullmatch(r"(`{3,}|~{3,})(?:json)?[ \t]*\r?\n(.*?)\r?\n\1[ \t]*", raw, re.I | re.S)
        if fenced is None:
            raise ValueError("JSON 代码围栏未完整闭合或包含额外内容")
        raw = fenced[2].strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 只允许一个完整对象被普通说明文字包围。首个对象若不完整，
        # 不继续寻找它的内部对象；前后仍有结构括号时也视为含糊输出。
        start = raw.find("{")
        if start < 0 or any(char in raw[:start] for char in "[]}"):
            raise ValueError("回复不是合法完整的 JSON 对象") from None
        try:
            data, stop = json.JSONDecoder().raw_decode(raw, start)
        except json.JSONDecodeError:
            raise ValueError("回复不是合法完整的 JSON 对象，不要截断或拼接结果") from None
        if any(char in raw[stop:] for char in "{}[]"):
            raise ValueError("回复包含多个对象或含糊的 JSON 内容，请只返回一个完整对象")

    if not isinstance(data, dict):
        raise ValueError("模型回复必须是 JSON 对象")
    return data


def _report_body(text: str) -> str:
    # 代码示例中的同名标题不是正式书目的起点。
    lines = []
    fence = ""
    fence_length = 0
    for line in text.strip().splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if marker:
            if not fence:
                fence, fence_length = marker[1][0], len(marker[1])
            elif marker[1][0] == fence and len(marker[1]) >= fence_length and not marker[2].strip():
                fence = ""
        elif not fence and re.fullmatch(r"##[ \t]+参考文献[ \t]*", line.rstrip("\r\n")):
            break
        lines.append(line)
    return "".join(lines).strip()


def _prose_mask(body: str) -> str:
    """遮住代码、数学和链接，保留字符位置，避免改写其中的示例引用。"""
    masked = list(body)

    def hide(start, stop):
        for index in range(start, stop):
            if masked[index] not in "\r\n":
                masked[index] = " "

    fence = ""
    fence_length = 0
    offset = 0
    for line in body.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence:
            hide(offset, offset + len(line))
            if marker and marker[1][0] == fence and len(marker[1]) >= fence_length and not marker[2].strip():
                fence = ""
        elif marker:
            fence, fence_length = marker[1][0], len(marker[1])
            hide(offset, offset + len(line))
        offset += len(line)

    patterns = (
        r"(?<!`)(`+)(?!`)(.*?)\1(?!`)",
        r"!?\[[^\]\n]*\]\([^\n)]*\)",
        r"\$\$.*?\$\$|(?<!\\)\$[^$\n]+\$|\\\[.*?\\\]|\\\(.*?\\\)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, "".join(masked), re.S):
            hide(match.start(), match.end())
    return "".join(masked)


def _paragraph_ranges(body: str):
    """空行分段；列表项、标题、表格行也各自保留引用位置。"""
    start = 0
    offset = 0
    boundary = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|[-+*][ \t]+|\d+[.)][ \t]+|>|\|)")
    for line in body.splitlines(keepends=True):
        if not line.strip():
            if start < offset:
                yield start, offset
            start = offset + len(line)
        elif boundary.match(line):
            if start < offset:
                yield start, offset
            start = offset
        offset += len(line)
    if start < len(body):
        yield start, len(body)


def _citation(value: str, sources: dict):
    """返回来源编号与合法标注；复杂页码只保留来源，不声称页码已核验。"""
    match = re.fullmatch(r"\s*(\d+(?:\s*[,，]\s*\d+)*)\s*(?:[,，]\s*p\.?\s*(\d+))?\s*", value, re.I)
    if match is not None:
        numbers = list(dict.fromkeys(int(number) for number in re.split(r"[,，]", match[1])))
        page = int(match[2]) if match[2] is not None else None
    else:
        complex_page = re.fullmatch(r"\s*(\d+)\s*[,，]\s*p\.?\s*(\d+(?:\s*[-–—,，]\s*\d+)+)\s*", value, re.I)
        if complex_page is None:
            raise ValueError("引用请使用 [1]、[1,2] 或 [1, p. 3] 格式")
        numbers = [int(complex_page[1])]
        # 页码范围与列表可省略为来源标注，不能把它们误当成论文编号。
        page = None

    for number in numbers:
        if number not in sources:
            raise ValueError(f"引用编号 {number} 不在指定材料中")
        if page is not None and page not in sources[number]["read_pages"]:
            raise ValueError(f"论文 {number} 的该页未实际读取，请使用已提供页码或仅写 [{number}]")
    label = ",".join(str(number) for number in numbers)
    if page is not None:
        label += f", p. {page}"
    # 同组来源顺序不同仍视为重复引用；不同单页引用保留各自定位。
    key = (tuple(sorted(numbers)), page)
    return numbers, f"[{label}]", key


def _normalize_citations(body: str, sources: dict):
    masked = _prose_mask(body)
    changes = []
    cited = set()
    count = 0
    for start, stop in _paragraph_ranges(body):
        citations = []
        for match in re.finditer(r"\[([^\[\]\n]*)\]", masked[start:stop]):
            if not re.match(r"\s*[+-]?\d", match[1]):
                continue
            numbers, label, key = _citation(match[1], sources)
            citations.append((start + match.start(), start + match.end(), numbers, label, key))
        # 保留该段最后一次相同标注；不跨自然段搬移，不删除不同页码的定位。
        last = {citation[4]: index for index, citation in enumerate(citations)}
        for index, (left, right, numbers, label, key) in enumerate(citations):
            if last[key] == index:
                count += 1
                cited.update(numbers)
                changes.append((left, right, label))
            else:
                changes.append((left, right, ""))

    parts = []
    offset = 0
    for left, right, replacement in changes:
        parts.extend((body[offset:left], replacement))
        offset = right
    parts.append(body[offset:])
    return "".join(parts), cited, count


def assemble_markdown(text: str, papers: list[dict], requirements: dict) -> str:
    """规范正文引用、核对赛事限制，再追加程序掌握的真实书目。"""
    body = _report_body(text)
    if not body.startswith("# "):
        raise ValueError("请直接输出以 # 标题开头的 Markdown 正文，不要 JSON 或外围代码围栏")
    sources = {paper["paper_id"]: paper for paper in papers}
    body, cited, count = _normalize_citations(body, sources)
    # 规范化后的正文才是交付内容；书目前的两个分隔换行同样计入上限。
    body_prefix = body + "\n\n"
    if not requirements["min_chars"] <= len(body_prefix) <= MAX_BODY_CHARS:
        raise ValueError(f"正文当前 {len(body_prefix)} 字符，须为 {requirements['min_chars']} 到 {MAX_BODY_CHARS} 字符（含书目前换行）")
    if count > MAX_CITATION_MARKERS:
        raise ValueError(f"正文引用标记共 {count} 次，最多 {MAX_CITATION_MARKERS} 次，请合并同段重复标注")
    references = [f"[{number}] {sources[number]['filename']}" for number in sorted(cited)]
    markdown = body_prefix + "## 参考文献\n\n" + "\n".join(references) + "\n"
    markdown.encode("utf-8")
    minimum = requirements["min_references"]
    if cited and len(cited) < minimum:
        missing = sorted(set(sources) - cited)
        raise CitationCoverageError(markdown, sorted(cited), missing, minimum)
    if not cited and len(cited) < minimum:
        raise ValueError(f"正文实际引用 0 篇，至少需要 {minimum} 篇指定论文")
    return markdown
