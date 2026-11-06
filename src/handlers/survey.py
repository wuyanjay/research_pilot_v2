"""C 类：提纲指导阅读，材料修订提纲，最后生成带来源的综述。"""

import json
from pathlib import Path
import re
import time

from src.handlers.survey_prompts import (
    OUTLINE_PROMPT, OVERVIEW_PROMPT, NOTES_PROMPT, COMPARISON_PROMPT,
    WRITING_PROMPT, build_messages,
)
from src.llm.client import LLMClient, LLMError, LLMTruncatedError
from src.tools import PDFTools
from src.tools.paper_reading import read_abstract

# 同一阶段所有批次共用一次追加重试；补读不会重置额度。
MAX_STAGE_RETRIES = 1
# 概览和材料整理的完整消息预算，组批时包含提示词、原题及上下文。
MAX_BATCH_PAPERS = 8
MAX_BATCH_CHARS = 24_000
REPAIR_FEEDBACK_CHARS = 1_000
# 页面分段保留相邻上下文；不截掉超长论文，也不丢弃其余分段。
PAGE_PART_CHARS = 6_000
PAGE_OVERLAP_CHARS = 200
MATERIAL_UNIT_CHARS = 10_000
# 全局比较、写作使用较大的工程字符预算，不代表模型 Token 上限。
MAX_GLOBAL_CHARS = 64_000
EVIDENCE_PAGES_PER_QUESTION = 3
MAX_SUPPLEMENT_ROUNDS = 1
# 参照平台 C 类整题预算分配请求时间，不额外包裹整题 wait_for。
TASK_BUDGET_S = 900.0
MAX_REQUEST_TIMEOUT_S = 180.0
DELIVERY_RESERVE_S = 10.0
# 正文按参考文献之前的完整字符计数，包括空白和 Markdown 标记。
MIN_BODY_CHARS = 800
MAX_BODY_CHARS = 5_000
MIN_REFERENCES = 5
MAX_CITATION_MARKERS = 40


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value, *, required=False) -> bool:
    return isinstance(value, list) and (not required or bool(value)) and all(_text(item) for item in value)


def _check_outline(data):
    if not isinstance(data, dict) or not _text(data.get("scope")) or not isinstance(data.get("sections"), list) or not data["sections"]:
        raise ValueError("提纲必须包含 scope 和非空 sections")
    sections = []
    for index, item in enumerate(data["sections"], 1):
        if (not isinstance(item, dict) or not _text(item.get("title"))
                or not _strings(item.get("questions"), required=True)
                or not _strings(item.get("information_needs"), required=True)):
            raise ValueError("每节必须有标题、阅读问题和信息需求")
        keywords = item.get("keywords")
        if (not isinstance(keywords, dict) or not _strings(keywords.get("zh"))
                or not _strings(keywords.get("en")) or not (keywords["zh"] or keywords["en"])):
            raise ValueError("每节须提供中英文关键词数组，至少有一个有效术语")
        sections.append({**item, "section_id": index})
    requirements = data.get("requirements")
    if not isinstance(requirements, dict):
        raise ValueError("提纲须提供 requirements")
    for name in ("min_chars", "min_references"):
        if type(requirements.get(name)) is not int or requirements[name] <= 0:
            raise ValueError(f"{name} 须为正整数")
    requirements = {
        "min_chars": max(MIN_BODY_CHARS, requirements["min_chars"]),
        "min_references": max(MIN_REFERENCES, requirements["min_references"]),
    }
    if requirements["min_chars"] > MAX_BODY_CHARS:
        raise ValueError("最低正文长度超过赛事 5000 字符上限，请核对原题要求")
    return {"scope": data["scope"], "sections": sections, "requirements": requirements}


def _paper_records(data, papers):
    """仅核对本批编号及覆盖，不判断论文事实是否正确。"""
    if not isinstance(data, dict) or not isinstance(data.get("papers"), list):
        raise ValueError("阶段结果须包含 papers 数组")
    expected = {paper["paper_id"] for paper in papers}
    seen = set()
    for item in data["papers"]:
        if not isinstance(item, dict) or type(item.get("paper_id")) is not int:
            raise ValueError("每篇结果须有程序提供的整数 paper_id")
        if item["paper_id"] not in expected or item["paper_id"] in seen:
            raise ValueError("论文编号不在本批内或重复")
        seen.add(item["paper_id"])
    if seen != expected:
        raise ValueError("阶段结果须覆盖本批全部论文")
    return data["papers"]


def _check_tasks(tasks, paper_ids=None):
    if not isinstance(tasks, list):
        raise ValueError("阅读任务必须是数组")
    for task in tasks:
        if not isinstance(task, dict) or not _text(task.get("question")) or not _strings(task.get("keywords"), required=True):
            raise ValueError("每个阅读任务须包含具体问题和关键词数组")
        if paper_ids is not None and (type(task.get("paper_id")) is not int or task["paper_id"] not in paper_ids):
            raise ValueError("补读任务使用了未授权论文编号")
    return tasks


def _check_overview(data, papers, outline):
    records = _paper_records(data, papers)
    sections = {item["section_id"] for item in outline["sections"]}
    for item in records:
        if not _text(item.get("research_focus")) or not isinstance(item.get("section_ids"), list):
            raise ValueError("论文概览须说明研究定位及涉及章节")
        if any(type(number) is not int or number not in sections for number in item["section_ids"]):
            raise ValueError("论文概览使用了不存在的章节编号")
        _check_tasks(item.get("reading_questions"))
        if not item["reading_questions"]:
            raise ValueError("每篇指定论文至少保留一个正文阅读任务")
    return records


def _check_notes(data, papers, outline):
    records = _paper_records(data, papers)
    sections = {item["section_id"] for item in outline["sections"]}
    for item in records:
        if not _text(item.get("research_focus")):
            raise ValueError("材料记录须说明研究问题、对象或场景")
        for name in ("methods", "conditions", "limitations", "gaps"):
            if not _strings(item.get(name)):
                raise ValueError(f"材料记录的 {name} 须为字符串数组，可为空")
        if not isinstance(item.get("findings"), list):
            raise ValueError("findings 须为数组，可为空并如实记录缺口")
        for finding in item["findings"]:
            if (not isinstance(finding, dict) or not _text(finding.get("statement"))
                    or finding.get("kind") not in ("reported_result", "author_interpretation", "review_inference")
                    or not isinstance(finding.get("section_ids"), list)
                    or any(type(number) is not int or number not in sections for number in finding["section_ids"])):
                raise ValueError("每项发现须有内容、信息性质及有效章节编号")
    return records


def _check_comparison(data, papers, supplement_allowed):
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list) or not data["sections"]:
        raise ValueError("比较结果须提供非空的修订 sections")
    allowed = {paper["paper_id"] for paper in papers}
    for section in data["sections"]:
        if (not isinstance(section, dict) or not _text(section.get("title"))
                or not isinstance(section.get("points"), list) or not _strings(section.get("gaps"))):
            raise ValueError("修订章节须包含标题、论述要点和缺口数组")
        for point in section["points"]:
            if (not isinstance(point, dict) or not _text(point.get("analysis"))
                    or not isinstance(point.get("paper_ids"), list)
                    or any(type(number) is not int or number not in allowed for number in point["paper_ids"])):
                raise ValueError("比较要点须包含分析和有效来源编号")
    tasks = _check_tasks(data.get("supplement_tasks", []), allowed)
    # 一轮补读之后只保留未解问题，不因模型再次提出任务而开启额外循环。
    return {"sections": data["sections"], "supplement_tasks": tasks if supplement_allowed else []}


def _prose(body):
    """代码、数学表达和 Markdown 链接里的数字不能充当正文引用。"""
    body = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", body)
    body = re.sub(r"`+[^`\n]*`+", "", body)
    body = re.sub(r"!?\[[^\]\n]*\]\([^\n)]*\)", "", body)
    body = re.sub(r"\$\$.*?\$\$|(?<!\\)\$[^$\n]+\$|\\\[.*?\\\]", "", body, flags=re.S)
    return body


def _report_body(text):
    # 只截去真正的文末书目；代码示例中的同名标题不应截断正文。
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


def assemble_markdown(text, papers, requirements):
    """核对正式正文要求并追加真实书目；不对事实解读做逐字摘录校验。"""
    body = _report_body(text)
    if not body.startswith("# ") or body.startswith("```"):
        raise ValueError("请直接输出以 # 标题开头的 Markdown 正文，不要 JSON 或外围代码围栏")
    # 平台按参考文献标题之前的精确前缀计数，分隔书目的两个换行也属于正文。
    body_prefix = body + "\n\n"
    if not requirements["min_chars"] <= len(body_prefix) <= MAX_BODY_CHARS:
        raise ValueError(f"正文当前 {len(body_prefix)} 字符，须为 {requirements['min_chars']} 到 {MAX_BODY_CHARS} 字符（含书目前换行）")
    sources = {paper["paper_id"]: paper for paper in papers}
    markers = re.findall(r"\[([^\[\]\n]*)\]", _prose(body))
    cited = set()
    count = 0
    for marker in markers:
        if not re.match(r"\s*[+-]?\d", marker):
            continue
        count += 1
        match = re.fullmatch(r"\s*(\d+(?:\s*[,，]\s*\d+)*)\s*(?:[,，]\s*p\.?\s*(\d+))?\s*", marker, re.I)
        if match is None:
            raise ValueError("引用请使用 [1]、[1,2] 或 [1, p. 3] 格式")
        numbers = [int(number) for number in re.split(r"[,，]", match[1])]
        for number in numbers:
            if number not in sources:
                raise ValueError(f"引用编号 {number} 不在指定材料中")
            if match[2] is not None and int(match[2]) not in sources[number]["read_pages"]:
                raise ValueError(f"论文 {number} 的该页未实际读取，请使用已提供页码或仅写 [{number}]")
            cited.add(number)
    if count > MAX_CITATION_MARKERS:
        raise ValueError(f"正文引用标记共 {count} 次，最多 {MAX_CITATION_MARKERS} 次，请合并同段重复标注")
    if len(cited) < requirements["min_references"]:
        raise ValueError(f"正文实际引用 {len(cited)} 篇，至少需要 {requirements['min_references']} 篇指定论文")
    references = [f"[{number}] {sources[number]['filename']}" for number in sorted(cited)]
    markdown = body_prefix + "## 参考文献\n\n" + "\n".join(references) + "\n"
    markdown.encode("utf-8")
    return markdown


class SurveyHandler:
    def __init__(self, llm: LLMClient, *, papers_directory: str | Path, work_directory: str | Path, team: str):
        self.llm = llm
        self.papers_directory = Path(papers_directory)
        self.work_directory = Path(work_directory)
        self.team = team

    async def _request(self, stage, messages, validate, retries, deadline, remaining_calls, *, json_reply=True, limit=MAX_BATCH_CHARS):
        original = messages
        while True:
            remaining = deadline - time.monotonic() - DELIVERY_RESERVE_S
            if remaining <= 0:
                raise TimeoutError("C 类任务剩余预算不足，停止新增模型请求")
            if len(_dump(messages)) > limit:
                raise ValueError(f"C 类{stage}输入超过字符预算，不能静默丢弃论文")
            timeout = min(self.llm.timeout_s, MAX_REQUEST_TIMEOUT_S, remaining / max(1, remaining_calls))
            try:
                text = await self.llm.complete(messages, temperature=0.2, max_retries=0, timeout_s=timeout)
                return validate(json.loads(text) if json_reply else text)
            except LLMTruncatedError:
                issue = "回复被截断，请精简并重新输出完整结果，不续写旧回复"
            except LLMError as error:
                if not error.retryable:
                    raise
                issue = str(error)
            except (ValueError, KeyError, TypeError) as error:
                issue = str(error)
            if retries["remaining"] == 0:
                raise ValueError(f"C 类{stage}失败，阶段重试已用尽：{issue}") from None
            retries["remaining"] -= 1
            remaining_calls = max(1, remaining_calls - 1)
            original_format = "JSON" if json_reply else "Markdown 正文"
            messages = original + [{"role": "user", "content": f"上一份结果不合规：{issue[:500]}。请按要求重新生成完整{original_format}。"}]

    def _check_time(self, deadline):
        if time.monotonic() >= deadline - DELIVERY_RESERVE_S:
            raise TimeoutError("C 类任务剩余预算不足，停止新增材料读取")

    def _units(self, paper):
        """保留所有原文分段；超长单页带重叠上下文，不按相关性丢弃论文。"""
        units = []
        current = []
        used = 0
        for page in paper["pages"]:
            text = page["text"]
            starts = range(0, max(1, len(text)), PAGE_PART_CHARS - PAGE_OVERLAP_CHARS)
            for start in starts:
                part = {**page, "text": text[start:start + PAGE_PART_CHARS], "start_char": start}
                if current and used + len(part["text"]) > MATERIAL_UNIT_CHARS:
                    units.append({**paper, "pages": current})
                    current = []
                    used = 0
                current.append(part)
                used += len(part["text"])
                if start + PAGE_PART_CHARS >= len(text):
                    break
        if current:
            units.append({**paper, "pages": current})
        return units

    def _batches(self, units, messages_builder):
        batches = []
        current = []
        for unit in units:
            trial = current + [unit]
            duplicate = any(item["paper_id"] == unit["paper_id"] for item in current)
            if current and (duplicate or len(trial) > MAX_BATCH_PAPERS
                    or len(_dump(messages_builder(trial))) + REPAIR_FEEDBACK_CHARS > MAX_BATCH_CHARS):
                batches.append(current)
                current = []
            if len(_dump(messages_builder([unit]))) + REPAIR_FEEDBACK_CHARS > MAX_BATCH_CHARS:
                raise ValueError("单个材料分段仍超过 C 类输入预算，不能丢弃该材料")
            current.append(unit)
        if current:
            batches.append(current)
        return batches

    async def _overview(self, question, outline, summaries, retries, deadline):
        units = []
        for summary in summaries:
            pages = summary["front_matter"] + summary["pages"]
            units.extend(self._units({key: value for key, value in {**summary, "pages": pages}.items() if key != "front_matter"}))
        builder = lambda batch: build_messages(OVERVIEW_PROMPT, question, outline=outline, papers=batch)
        batches = self._batches(units, builder)
        merged = {}
        for index, batch in enumerate(batches):
            records = await self._request(
                "论文概览", builder(batch), lambda data: _check_overview(data, batch, outline),
                retries, deadline, len(batches) - index + 6,
            )
            for record in records:
                item = merged.setdefault(record["paper_id"], {"paper_id": record["paper_id"], "research_focus": [], "section_ids": [], "reading_questions": []})
                for name in ("section_ids", "reading_questions"):
                    for value in record[name]:
                        if value not in item[name]:
                            item[name].append(value)
                if record["research_focus"] not in item["research_focus"]:
                    item["research_focus"].append(record["research_focus"])
        return list(merged.values())

    def _read_tasks(self, tools, sources, tasks, deadline, existing=None):
        existing = existing or {}
        readings = []
        for paper in sources:
            queries = [task for task in tasks if task["paper_id"] == paper["paper_id"]]
            if not queries:
                continue
            selected = set()
            searched = set()
            for query in queries:
                self._check_time(deadline)
                key = tuple(query["keywords"])
                if key in searched:
                    continue
                searched.add(key)
                hits = tools.search_pdf_text([paper["path"]], query["keywords"], limit=paper["total_pages"])
                later = [hit for hit in hits if hit["page"] not in paper["summary_pages"]]
                for hit in (later or hits)[:EVIDENCE_PAGES_PER_QUESTION]:
                    # 相邻页可包含跨页段落、表注、指标定义，合并后统一读取。
                    selected.update(range(max(1, hit["page"] - 1), min(paper["total_pages"], hit["page"] + 1) + 1))
            if not selected:
                selected = set(range(1, paper["total_pages"] + 1))
            selected.difference_update(existing.get(paper["paper_id"], set()))
            if not selected:
                continue
            self._check_time(deadline)
            pages = tools.read_pdf_pages(paper["path"], sorted(selected))["pages"]
            if not any(page["text"].strip() for page in pages):
                raise ValueError(f"指定论文未提取到可阅读正文：{paper['filename']}")
            readings.append({"paper_id": paper["paper_id"], "filename": paper["filename"], "pages": pages})
        return readings

    async def _notes(self, question, outline, readings, notes, retries, deadline):
        units = []
        for paper in readings:
            units.extend(self._units(paper))
        builder = lambda batch: build_messages(NOTES_PROMPT, question, outline=outline, papers=batch)
        batches = self._batches(units, builder)
        for index, batch in enumerate(batches):
            records = await self._request(
                "材料整理", builder(batch), lambda data: _check_notes(data, batch, outline),
                retries, deadline, len(batches) - index + 4,
            )
            by_id = {paper["paper_id"]: paper for paper in batch}
            for record in records:
                # TODO：模型提炼的事实与分析尚未做语义核验，不能标记为已验证事实。
                item = notes.setdefault(record["paper_id"], {
                    "paper_id": record["paper_id"], "research_focus": [], "methods": [], "conditions": [],
                    "findings": [], "limitations": [], "gaps": [], "source_pages": [],
                })
                if record["research_focus"] not in item["research_focus"]:
                    item["research_focus"].append(record["research_focus"])
                for name in ("methods", "conditions", "findings", "limitations", "gaps"):
                    for value in record[name]:
                        if value not in item[name]:
                            item[name].append(value)
                for page in by_id[record["paper_id"]]["pages"]:
                    if page["page"] not in item["source_pages"]:
                        item["source_pages"].append(page["page"])

    async def _compare(self, question, outline, sources, notes, retries, deadline, supplement_allowed):
        context = {"outline": outline, "notes": list(notes.values()), "supplement_allowed": supplement_allowed}
        messages = build_messages(COMPARISON_PROMPT, question, **context)
        if len(_dump(messages)) + REPAIR_FEEDBACK_CHARS <= MAX_GLOBAL_CHARS:
            return await self._request(
                "跨论文比较", messages, lambda data: _check_comparison(data, sources, supplement_allowed),
                retries, deadline, 2 + retries["remaining"] + MAX_STAGE_RETRIES, limit=MAX_GLOBAL_CHARS,
            )
        # 全局材料较多时按章节比较，每节仍同时考虑所有论文，不拼接各批论文排名。
        combined = {"sections": [], "supplement_tasks": []}
        for section in outline["sections"]:
            chapter_notes = []
            for note in notes.values():
                findings = [item for item in note["findings"] if not item["section_ids"] or section["section_id"] in item["section_ids"]]
                chapter_notes.append({**note, "findings": findings})
            messages = build_messages(COMPARISON_PROMPT, question,
                outline={**outline, "sections": [section]}, notes=chapter_notes, supplement_allowed=supplement_allowed)
            result = await self._request(
                "跨论文比较", messages, lambda data: _check_comparison(data, sources, supplement_allowed),
                retries, deadline, 3, limit=MAX_GLOBAL_CHARS,
            )
            combined["sections"].extend(result["sections"])
            for task in result["supplement_tasks"]:
                if task not in combined["supplement_tasks"]:
                    combined["supplement_tasks"].append(task)
        return combined

    def _writing_messages(self, question, outline, comparison, sources, notes, readings):
        # 保留全部论文的结构化记录；原文按剩余空间逐页附加，不重复塞入全部 PDF。
        context = {
            "requirements": outline["requirements"], "outline": comparison["sections"],
            "notes": list(notes.values()),
            "sources": [{key: paper[key] for key in ("paper_id", "filename", "read_pages")} for paper in sources],
            "pages": [],
        }
        if len(_dump(build_messages(WRITING_PROMPT, question, **context))) + REPAIR_FEEDBACK_CHARS > MAX_GLOBAL_CHARS:
            # 长记录已在比较阶段被逐章考虑。写作改用该阶段选定的要点，仍保留每篇的条件与局限。
            selected_notes = []
            for note in notes.values():
                item = {key: note[key] for key in ("paper_id", "research_focus", "methods", "conditions", "limitations", "source_pages")}
                item["comparison_points"] = [
                    point["analysis"]
                    for section in comparison["sections"]
                    for point in section["points"]
                    if note["paper_id"] in point["paper_ids"]
                ]
                selected_notes.append(item)
            context["notes"] = selected_notes
            context["material_scope"] = "分段记录较长，使用跨论文比较阶段选定的论述要点，并保留全部论文的条件与局限。"
        # 轮流为各篇补充实际原文，避免输入空间被第一篇论文占满。
        depth = max((len(reading["pages"]) for reading in readings), default=0)
        for index in range(depth):
            for reading in readings:
                if index >= len(reading["pages"]):
                    continue
                page = reading["pages"][index]
                entry = {"paper_id": reading["paper_id"], **page}
                trial = {**context, "pages": context["pages"] + [entry]}
                if len(_dump(build_messages(WRITING_PROMPT, question, **trial))) + REPAIR_FEEDBACK_CHARS <= MAX_GLOBAL_CHARS:
                    context["pages"].append(entry)
        return build_messages(WRITING_PROMPT, question, **context)

    async def handle(self, question: dict) -> dict:
        deadline = time.monotonic() + TASK_BUDGET_S
        # 所有状态属于当前请求；同一阶段在分批和补读后仍使用同一份余额。
        retries = {name: {"remaining": MAX_STAGE_RETRIES} for name in ("outline", "overview", "notes", "comparison", "writing")}

        # 一、先想清楚文章写什么，再按章节拆解阅读问题。
        outline = await self._request(
            "暂定提纲", build_messages(OUTLINE_PROMPT, question), _check_outline,
            retries["outline"], deadline, 5 * (1 + MAX_STAGE_RETRIES),
        )

        # 二、只定位本题指定材料；编号由程序固定，不借用 B 类的全库模式。
        tools = PDFTools(question["materials"], self.papers_directory, self.work_directory / "pdf_cache")
        sources = tools.list_papers()
        if len(sources) < outline["requirements"]["min_references"]:
            raise ValueError("指定论文数量不足以满足引用要求，不能降低篇数或编造文献")
        summaries = []
        for number, paper in enumerate(sources, 1):
            self._check_time(deadline)
            summary = read_abstract(tools, paper)
            paper.update(paper_id=number, total_pages=summary["total_pages"],
                         summary_pages=sorted({page["page"] for page in summary["front_matter"] + summary["pages"]}), read_pages=[])
            summaries.append({**summary, "paper_id": number})

        # 三、概览每篇论文的分工；四、围绕具体问题补读正文。
        overview = await self._overview(question, outline, summaries, retries["overview"], deadline)
        tasks = [{**query, "paper_id": paper["paper_id"]} for paper in overview for query in paper["reading_questions"]]
        readings = self._read_tasks(tools, sources, tasks, deadline)

        # 五、形成带来源的文献记录；六、比较条件和结果，修订提纲。
        notes = {}
        await self._notes(question, outline, readings, notes, retries["notes"], deadline)
        comparison = await self._compare(question, outline, sources, notes, retries["comparison"], deadline, True)
        for _ in range(MAX_SUPPLEMENT_ROUNDS):
            if not comparison["supplement_tasks"]:
                break
            already_read = {paper["paper_id"]: {page["page"] for page in paper["pages"]} for paper in readings}
            extra = self._read_tasks(tools, sources, comparison["supplement_tasks"], deadline, already_read)
            await self._notes(question, outline, extra, notes, retries["notes"], deadline)
            readings.extend(extra)
            comparison = await self._compare(question, outline, sources, notes, retries["comparison"], deadline, False)

        # 七、直接生成 Markdown 正文；程序追加真实参考文献，不让模型生成路径。
        for paper in sources:
            paper["read_pages"] = sorted({page["page"] for reading in readings if reading["paper_id"] == paper["paper_id"] for page in reading["pages"]})
        markdown = await self._request(
            "综述写作", self._writing_messages(question, outline, comparison, sources, notes, readings),
            lambda text: assemble_markdown(text, sources, outline["requirements"]),
            retries["writing"], deadline, 1 + MAX_STAGE_RETRIES, json_reply=False, limit=MAX_GLOBAL_CHARS,
        )

        # 八、交给现有 Agent 调用公共交付组件，写入及回读成功才返回 HTTP 答卷。
        return {
            "case_id": question["case_id"],
            "path": f"data/expert/{question['case_id']}-综述-{self.team}.md",
            "markdown": markdown,
        }
