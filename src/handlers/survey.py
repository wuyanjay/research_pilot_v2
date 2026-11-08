"""C 类：提纲指导阅读，材料修订提纲，最后生成带来源的综述。"""

import json
import logging
from pathlib import Path
import time

from src.handlers.survey_prompts import (
    OUTLINE_PROMPT, OVERVIEW_PROMPT, NOTES_PROMPT, COMPARISON_PROMPT,
    WRITING_PROMPT, COVERAGE_REPAIR_PROMPT, build_messages,
)
from src.handlers.survey_format import (
    MAX_BODY_CHARS, MAX_CITATION_MARKERS, CitationCoverageError, _report_body, assemble_markdown, parse_json_object,
)
from src.llm.client import LLMClient, LLMError, LLMTruncatedError
from src.tools import PDFTools
from src.tools.paper_reading import read_abstract

# 同一阶段所有批次共用一次追加重试；补读不会重置额度。
MAX_STAGE_RETRIES = 1
# 完整消息的字符预算，包括提示词、原题、上下文及修复反馈空间。
MAX_BATCH_PAPERS = 8
MAX_BATCH_CHARS = 24_000
REPAIR_FEEDBACK_CHARS = 1_000
# 概览保留有限实际原文，截短时明确标记，未解信息留待正文核验。
OVERVIEW_TEXT_CHARS = 3_500
OVERVIEW_FRONT_CHARS = 600
FAILED_REPLY_CHARS = 400
# 覆盖提示只复用简短完整论点；较长条目整体跳过，不截断句子。
COVERAGE_FINDING_CHARS = 240
COVERAGE_COMPARISON_CHARS = 300
# 长页分段保留重叠上下文；同篇分段可在总消息预算内合批。
PAGE_PART_CHARS = 6_000
PAGE_OVERLAP_CHARS = 200
MATERIAL_UNIT_CHARS = 10_000
MAX_GLOBAL_CHARS = 64_000
# 按论文控制累计读取量，跨页疑点留到一次定向补读。
INITIAL_BODY_PAGES = 3
SUPPLEMENT_BODY_PAGES = 2
MAX_SUPPLEMENT_ROUNDS = 1
# 赛事 C 类总预算，不额外包裹整题 wait_for。
TASK_BUDGET_S = 900.0
MAX_REQUEST_TIMEOUT_S = 180.0
DELIVERY_RESERVE_S = 10.0
# 工程预留仍属于同一个总预算，不是赛事额外时限。
WRITING_RESERVE_S = 150.0
COMPARISON_RESERVE_S = 90.0
INITIAL_NOTES_RESERVE_S = 180.0
OVERVIEW_RESERVE_S = 60.0
# 实测成本用于评估工作量；不足可执行窗口时先缩减可选读取。
REQUEST_ESTIMATE_S = 60.0
MIN_REQUEST_WINDOW_S = 30.0
MIN_BODY_CHARS = 800
MIN_REFERENCES = 5

logger = logging.getLogger(__name__)


class ModelStructureError(ValueError):
    """模型结构可修复错误，携带本次已安全接受的记录。"""

    def __init__(self, message, partial_records=None, field_paths=None):
        super().__init__(message)
        self.partial_records = list(partial_records or [])
        self.field_paths = list(field_paths or [])


class StageFailure(Exception):
    """共享重试或预算用尽后的可恢复阶段失败。"""

    def __init__(self, stage, reason, partial_records=None):
        super().__init__(stage)
        self.stage = stage
        self.reason = reason
        self.partial_records = list(partial_records or [])


class StageBudgetError(StageFailure):
    """阶段自身预算不足；不用于吞掉PDF/系统IO超时。"""


def _merge_partial_record(old, new):
    """合并同篇的部分结构记录，保留先前有效字段和列表项。"""
    if old is None:
        return dict(new)
    merged = dict(old)
    for key, value in new.items():
        if isinstance(value, list):
            existing = merged.setdefault(key, [])
            if isinstance(existing, list):
                for item in value:
                    if item not in existing:
                        existing.append(item)
        elif value not in (None, ""):
            if merged.get(key) in (None, ""):
                merged[key] = value
    return merged


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strings(value, *, required=False) -> bool:
    return isinstance(value, list) and (not required or bool(value)) and all(_text(item) for item in value)


def _unique_words(words):
    result = []
    seen = set()
    for word in words:
        key = word.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(word.strip())
    return result


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
        sections.append({**item, "section_id": index, "question_id": f"Q{index}"})
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
    # 每节合并成一个固定编号的阅读问题，避免分段后反复生成相近任务。
    questions = []
    for section in sections:
        questions.append({
            "question_id": section["question_id"], "section_id": section["section_id"],
            "question": "；".join(_unique_words(section["questions"])),
            "keywords": _unique_words(section["keywords"]["zh"] + section["keywords"]["en"]),
        })
    return {
        "scope": data["scope"], "sections": sections,
        "reading_questions": questions, "requirements": requirements,
    }


def _paper_records(data, papers):
    """接受本批内唯一授权编号，拒绝歧义编号。"""
    if not isinstance(data, dict) or not isinstance(data.get("papers"), list):
        raise ModelStructureError("papers 数组缺失或格式错误")
    expected = {paper["paper_id"] for paper in papers}
    counts = {}
    issues = []
    for item in data["papers"]:
        if not isinstance(item, dict) or type(item.get("paper_id")) is not int:
            issues.append("papers[].paper_id")
            continue
        counts[item["paper_id"]] = counts.get(item["paper_id"], 0) + 1
        if item["paper_id"] not in expected:
            issues.append("papers[].paper_id")
    duplicates = {paper_id for paper_id, count in counts.items() if count > 1}
    issues.extend("papers[].paper_id" for _ in duplicates)
    accepted = [item for item in data["papers"] if isinstance(item, dict)
                and type(item.get("paper_id")) is int and item["paper_id"] in expected
                and item["paper_id"] not in duplicates]
    missing = expected - {item["paper_id"] for item in accepted}
    return accepted, issues, sorted(missing)


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
    records, id_issues, missing = _paper_records(data, papers)
    questions = {item["question_id"]: item for item in outline["reading_questions"]}
    cleaned = []
    issues = list(id_issues)
    for item in records:
        known = item.get("known_information", [])
        pending = item.get("reading_questions", [])
        if "known_information" not in item:
            issues.append(f"papers[{item['paper_id']}].known_information")
        if "reading_questions" not in item:
            issues.append(f"papers[{item['paper_id']}].reading_questions")
        if not isinstance(known, list):
            issues.append(f"papers[{item['paper_id']}].known_information")
            known = []
        if not isinstance(pending, list):
            issues.append(f"papers[{item['paper_id']}].reading_questions")
            pending = []
        valid_known = []
        for information in known:
            if (not isinstance(information, dict) or not isinstance(information.get("question_id"), str)
                    or information.get("question_id") not in questions
                    or not _text(information.get("information"))):
                issues.append(f"papers[{item['paper_id']}].known_information[]")
                continue
            valid_known.append(information)
        merged = {}
        for task in pending:
            if (not isinstance(task, dict) or not isinstance(task.get("question_id"), str)
                    or task.get("question_id") not in questions
                    or not _strings(task.get("keywords"), required=True)):
                issues.append(f"papers[{item['paper_id']}].reading_questions[]")
                continue
            question = questions[task["question_id"]]
            record = merged.setdefault(task["question_id"], {
                "question_id": task["question_id"], "question": question["question"], "keywords": [],
            })
            record["keywords"] = _unique_words(record["keywords"] + task["keywords"])
        focus = item.get("research_focus", "")
        if not isinstance(focus, str) or ("research_focus" in item and not _text(focus)):
            issues.append(f"papers[{item['paper_id']}].research_focus")
            focus = ""
        cleaned.append({"paper_id": item["paper_id"], "research_focus": focus,
                        "known_information": valid_known, "reading_questions": list(merged.values())})
    if issues or missing:
        logger.warning("C 类结构校验 %s", _dump({"event": "record_validation", "field_paths": issues,
                      "accepted": len(cleaned), "rejected": len(issues), "missing_ids": missing}))
        raise ModelStructureError("概览记录有无效字段", cleaned,
                                  issues + (["papers[].paper_id"] if missing else []))
    return cleaned


def _check_notes(data, papers, outline):
    records, id_issues, missing = _paper_records(data, papers)
    sections = {item["section_id"] for item in outline["sections"]}
    cleaned = []
    issues = list(id_issues)
    for item in records:
        record = {"paper_id": item["paper_id"], "research_focus": item.get("research_focus", "")}
        for name in ("methods", "conditions", "limitations", "gaps"):
            value = item.get(name, [])
            if name not in item:
                issues.append(f"papers[{item['paper_id']}].{name}")
            if not _strings(value):
                issues.append(f"papers[{item['paper_id']}].{name}")
                value = []
            record[name] = value
        findings = item.get("findings", [])
        if "findings" not in item:
            issues.append(f"papers[{item['paper_id']}].findings")
        if not isinstance(findings, list):
            issues.append(f"papers[{item['paper_id']}].findings")
            findings = []
        valid_findings = []
        ignored_fields = []
        for finding in findings:
            if (not isinstance(finding, dict) or not _text(finding.get("statement"))
                    or finding.get("kind") not in ("reported_result", "author_interpretation", "review_inference")):
                issues.append(f"papers[{item['paper_id']}].findings[]")
                continue
            if (not isinstance(finding.get("section_ids"), list)
                    or any(type(number) is not int or number not in sections for number in finding["section_ids"])):
                ignored_fields.append(f"papers[{item['paper_id']}].findings[].section_ids")
                finding = {**finding, "section_ids": []}
            valid_findings.append(finding)
        record["findings"] = valid_findings
        if ignored_fields:
            record["_field_paths"] = ignored_fields
        cleaned.append(record)
    if issues or missing:
        logger.warning("C 类结构校验 %s", _dump({"event": "record_validation", "field_paths": issues,
                      "accepted": len(cleaned), "rejected": len(issues), "missing_ids": missing}))
        raise ModelStructureError("材料记录有无效字段", cleaned,
                                  issues + (["papers[].paper_id"] if missing else []))
    return cleaned


def _check_comparison(data, papers, supplement_allowed):
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ModelStructureError("比较结果须提供非空的修订 sections", field_paths=["sections"])
    allowed = {paper["paper_id"] for paper in papers}
    sections = []
    issues = []
    for section in data["sections"]:
        if (not isinstance(section, dict) or not _text(section.get("title"))
                or not isinstance(section.get("points"), list) or not _strings(section.get("gaps"))):
            issues.append("sections[]")
            continue
        points = []
        for point in section["points"]:
            if (not isinstance(point, dict) or not _text(point.get("analysis"))
                    or not isinstance(point.get("paper_ids"), list)
                    or any(type(number) is not int or number not in allowed for number in point["paper_ids"])):
                issues.append("sections[].points[]")
                continue
            points.append(point)
        sections.append({**section, "points": points})
    if not sections:
        raise ModelStructureError("比较结果没有可用章节", field_paths=["sections[]"])
    if issues:
        logger.warning("C 类结构校验 %s", _dump({"event": "record_validation", "field_paths": issues,
                      "accepted": len(sections), "rejected": len(issues)}))
    raw_tasks = data.get("supplement_tasks", []) if supplement_allowed else []
    if not isinstance(raw_tasks, list):
        logger.warning("C 类结构校验 %s", _dump({"event": "record_validation",
                      "field_paths": ["supplement_tasks"], "accepted": 0, "rejected": 1}))
        raw_tasks = []
    tasks = []
    for task in raw_tasks:
        try:
            tasks.extend(_check_tasks([task], allowed))
        except ValueError:
            logger.warning("C 类结构校验 %s", _dump({"event": "record_validation",
                          "field_paths": ["supplement_tasks[]"], "accepted": len(tasks), "rejected": 1}))
    # 一轮补读之后只保留未解问题，不因模型再次提出任务而开启额外循环。
    return {"sections": sections, "supplement_tasks": tasks if supplement_allowed else []}


class SurveyHandler:
    def __init__(self, llm: LLMClient, *, papers_directory: str | Path, work_directory: str | Path, team: str):
        self.llm = llm
        self.papers_directory = Path(papers_directory)
        self.work_directory = Path(work_directory)
        self.team = team

    def _future_reserve(self, stage):
        reserve = 0.0
        if stage != "综述写作":
            reserve += WRITING_RESERVE_S
        if stage in ("暂定提纲", "论文概览", "材料整理"):
            reserve += COMPARISON_RESERVE_S
        if stage in ("暂定提纲", "论文概览"):
            reserve += INITIAL_NOTES_RESERVE_S
        if stage == "暂定提纲":
            reserve += OVERVIEW_RESERVE_S
        return reserve

    def _request_estimate(self, retries):
        durations = retries.get("durations", [])
        if not durations:
            return REQUEST_ESTIMATE_S
        recent = durations[-3:]
        return max(MIN_REQUEST_WINDOW_S, min(90.0, sum(recent) / len(recent)))

    async def _request(self, stage, messages, validate, retries, deadline, remaining_calls, *, json_reply=True, limit=MAX_BATCH_CHARS, coverage=None, recoverable=False, diagnostic=None):
        original = messages
        attempt = 0
        partial_records = {}
        while True:
            remaining = deadline - time.monotonic() - DELIVERY_RESERVE_S
            available = remaining - self._future_reserve(stage)
            # 只预留本阶段其他批次的实测成本，不机械均分为几秒钟等待。
            window = available - max(0, remaining_calls - 1) * self._request_estimate(retries)
            if window < MIN_REQUEST_WINDOW_S:
                if coverage is not None and coverage.get("candidate"):
                    self._log_coverage_fallback(coverage, attempt, "budget")
                    return coverage["candidate"].markdown
                if recoverable:
                    raise StageBudgetError(stage, "budget", partial_records.values())
                raise TimeoutError(f"C 类{stage}预算不足，需减少本轮工作量，停止新增模型请求")
            if len(_dump(messages)) > limit:
                if recoverable:
                    raise StageBudgetError(stage, "input_too_large", partial_records.values())
                if coverage is not None and coverage.get("candidate"):
                    self._log_coverage_fallback(coverage, attempt, "input_too_large")
                    return coverage["candidate"].markdown
                raise ValueError(f"C 类{stage}输入超过字符预算，不能静默丢弃论文")
            timeout = min(self.llm.timeout_s, MAX_REQUEST_TIMEOUT_S, window)
            attempt += 1
            started = time.monotonic()
            text = None
            failure = "interrupted"
            issue = ""
            validating = False
            logger.info("C 类请求 %s", _dump({
                "event": "request_start", "stage": stage, "attempt": attempt,
                "remaining_calls": remaining_calls, "input_chars": len(_dump(messages)),
                "remaining_s": round(remaining, 3), "timeout_s": round(timeout, 3),
                **(diagnostic or {}),
            }))
            try:
                text = await self.llm.complete(messages, temperature=0.2, max_retries=0, timeout_s=timeout)
                if json_reply:
                    try:
                        parsed = parse_json_object(text)
                    except ValueError:
                        raise ModelStructureError("模型JSON格式无效") from None
                else:
                    parsed = text
                validating = True
                result = validate(parsed)
                if isinstance(result, list):
                    ignored = [path for record in result if isinstance(record, dict)
                               for path in record.pop("_field_paths", [])]
                    if ignored:
                        logger.warning("C 类字段忽略 %s", _dump({"event": "field_ignored",
                                      "case_id": (diagnostic or {}).get("case_id"),
                                      "batch": (diagnostic or {}).get("batch"),
                                      "paper_ids": (diagnostic or {}).get("paper_ids"),
                                      "field_paths": ignored}))
                if recoverable and isinstance(result, list) and partial_records:
                    for record in result:
                        if isinstance(record, dict) and type(record.get("paper_id")) is int:
                            paper_id = record["paper_id"]
                            partial_records[paper_id] = _merge_partial_record(partial_records.get(paper_id), record)
                    result = list(partial_records.values())
                failure = None
                return result
            except CitationCoverageError as error:
                if coverage is None:
                    raise
                best = coverage.get("candidate")
                if best is None or len(error.cited) > len(best.cited):
                    coverage["candidate"] = error
                failure = "citation_coverage"
                issue = str(error)
            except ModelStructureError as error:
                logger.warning("C 类结构校验 %s", _dump({"event": "record_validation",
                              "case_id": (diagnostic or {}).get("case_id"),
                              "batch": (diagnostic or {}).get("batch"),
                              "paper_ids": (diagnostic or {}).get("paper_ids"),
                              "field_paths": error.field_paths, "accepted": len(error.partial_records),
                              "rejected": len(error.field_paths)}))
                for record in error.partial_records:
                    if isinstance(record, dict) and type(record.get("paper_id")) is int:
                        paper_id = record["paper_id"]
                        partial_records[paper_id] = _merge_partial_record(partial_records.get(paper_id), record)
                failure = "format"
                issue = str(error)
            except LLMTruncatedError:
                failure = "truncated"
                issue = "回复被截断，请精简并重新输出完整结果，不续写旧回复"
            except LLMError as error:
                failure = "request"
                if not error.retryable:
                    raise
                issue = str(error)
            except (ValueError, KeyError, TypeError) as error:
                if recoverable or (coverage is not None and (not validating or isinstance(error, (KeyError, TypeError)))):
                    raise
                failure = "format"
                issue = str(error)
            finally:
                elapsed = time.monotonic() - started
                retries.setdefault("durations", []).append(elapsed)
                # 仅记录数量、耗时和错误类别，不写请求、模型正文或认证配置。
                logger.info("C 类请求 %s", _dump({
                    "event": "request_end", "stage": stage, "attempt": attempt,
                    "elapsed_s": round(elapsed, 3),
                    "output_chars": len(text) if isinstance(text, str) else 0,
                    "failure": failure,
                    **(diagnostic or {}),
                }))
            if retries["remaining"] == 0:
                if coverage is not None and coverage.get("candidate"):
                    self._log_coverage_fallback(coverage, attempt, failure)
                    return coverage["candidate"].markdown
                if recoverable:
                    if failure == "budget":
                        raise StageBudgetError(stage, "budget", partial_records.values()) from None
                    raise StageFailure(stage, failure or "format", partial_records.values()) from None
                raise ValueError(f"C 类{stage}失败，阶段重试已用尽：{issue}") from None
            retries["remaining"] -= 1
            if failure == "request":
                # 请求没有完成，没有一份回复需要格式修复；保留原消息重发。
                messages = original
                continue
            if failure == "citation_coverage":
                candidate = coverage["candidate"]
                repair = self._coverage_repair_messages(coverage["question"], coverage["sources"], candidate,
                                                        coverage["notes"], coverage["comparison"], coverage["requirements"])
                if repair is None or len(_dump(repair)) > limit:
                    self._log_coverage_fallback(coverage, attempt, "repair_too_large")
                    return candidate.markdown
                messages = repair
                continue
            original_format = "JSON" if json_reply else "Markdown 正文"
            messages = list(original)
            if text:
                messages.append({"role": "assistant", "content": text[:FAILED_REPLY_CHARS]})
            messages.append({
                "role": "user", "content": f"上一份结果不合规：{issue[:250]}。请按要求重新生成完整{original_format}。",
            })

    def _check_time(self, deadline):
        if time.monotonic() >= deadline - DELIVERY_RESERVE_S:
            raise StageBudgetError("C 类任务", "budget")

    def _log_coverage_fallback(self, coverage, attempt, reason):
        candidate = coverage["candidate"]
        logger.warning("C 类引用覆盖回退 %s", _dump({
            "case_id": coverage["question"]["case_id"], "cited": candidate.cited,
            "missing": candidate.missing, "target": candidate.min_references,
            "attempt": attempt, "reason": reason,
        }))

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
            # 同篇分段合成一个对象，保留所有页面及 start_char，避免 by_id 覆盖。
            trial = [{**paper, "pages": list(paper["pages"])} for paper in current]
            same = next((paper for paper in trial if paper["paper_id"] == unit["paper_id"]), None)
            if same is None:
                trial.append({**unit, "pages": list(unit["pages"])})
            else:
                for page in unit["pages"]:
                    if page not in same["pages"]:
                        same["pages"].append(page)
            if current and (len(trial) > MAX_BATCH_PAPERS
                    or len(_dump(messages_builder(trial))) + REPAIR_FEEDBACK_CHARS > MAX_BATCH_CHARS):
                batches.append(current)
                trial = [{**unit, "pages": list(unit["pages"])}]
            if len(_dump(messages_builder([unit]))) + REPAIR_FEEDBACK_CHARS > MAX_BATCH_CHARS:
                raise ValueError("单个材料分段仍超过 C 类输入预算，不能丢弃该材料")
            current = trial
        if current:
            batches.append(current)
        return batches

    def _overview_excerpt(self, summary):
        result = {key: value for key, value in summary.items() if key != "front_matter"}
        pages = []
        truncated = False
        for group, allowance in ((summary["front_matter"], OVERVIEW_FRONT_CHARS),
                                 (summary["pages"], OVERVIEW_TEXT_CHARS)):
            for page in group:
                text = page["text"][:max(0, allowance)]
                allowance -= len(text)
                if len(text) < len(page["text"]):
                    truncated = True
                if text.strip():
                    pages.append({**page, "text": text})
        result["pages"] = pages
        result["overview_truncated"] = truncated
        return result

    async def _overview(self, question, outline, summaries, retries, deadline):
        units = [self._overview_excerpt(summary) for summary in summaries]
        builder = lambda batch: build_messages(OVERVIEW_PROMPT, question, outline=outline, papers=batch)
        batches = self._batches(units, builder)
        merged = {}
        for index, batch in enumerate(batches):
            try:
                records = await self._request(
                    "论文概览", builder(batch), lambda data: _check_overview(data, batch, outline),
                    retries, deadline, len(batches) - index, recoverable=True,
                    diagnostic={"case_id": question["case_id"], "batch": index + 1,
                                "paper_ids": [paper["paper_id"] for paper in batch]},
                )
            except StageFailure as failure:
                records = failure.partial_records
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "overview",
                              "case_id": question["case_id"], "batch": index + 1,
                              "record_count": len(records), "reason": failure.reason}))
            for record in records:
                item = merged.setdefault(record["paper_id"], {
                    "paper_id": record["paper_id"], "research_focus": [],
                    "known_information": [], "reading_questions": [],
                })
                for information in record["known_information"]:
                    if information not in item["known_information"]:
                        item["known_information"].append(information)
                for task in record["reading_questions"]:
                    same = next((value for value in item["reading_questions"]
                                 if value["question_id"] == task["question_id"]), None)
                    if same is None:
                        item["reading_questions"].append(dict(task))
                    else:
                        same["keywords"] = _unique_words(same["keywords"] + task["keywords"])
                if record.get("research_focus") and record["research_focus"] not in item["research_focus"]:
                    item["research_focus"].append(record["research_focus"])
        return list(merged.values())

    def _read_tasks(self, tools, sources, tasks, deadline, existing=None):
        supplement = existing is not None
        existing = existing or {}
        page_limit = SUPPLEMENT_BODY_PAGES if supplement else INITIAL_BODY_PAGES
        readings = []
        for paper in sources:
            queries = [task for task in tasks if task["paper_id"] == paper["paper_id"]]
            if supplement and not queries:
                continue
            self._check_time(deadline)
            old_pages = existing.get(paper["paper_id"], set())
            terms = _unique_words([word for query in queries for word in query["keywords"]])
            hits = tools.search_pdf_text([paper["path"]], terms, limit=paper["total_pages"]) if terms else []
            selected = []
            candidates = []
            unmatched = []
            for query in queries:
                words = {word.casefold() for word in query["keywords"]}
                matched = [hit for hit in hits if words.intersection(
                    word.casefold() for word in hit["matched_keywords"])]
                matched.sort(key=lambda hit: (
                    hit["page"] in paper["summary_pages"],
                    -len(words.intersection(word.casefold() for word in hit["matched_keywords"])),
                    hit["page"],
                ))
                available = [hit["page"] for hit in matched if hit["page"] not in old_pages]
                if not available:
                    unmatched.append(query["question"])
                for page in available:
                    if page not in candidates:
                        candidates.append(page)
                # 不同阅读问题先各选一个定位页，然后用剩余额度补充证据。
                first = next((page for page in available if page not in selected), None)
                if first is not None and len(selected) < page_limit:
                    selected.append(first)
            for page in candidates:
                if page not in selected and len(selected) < page_limit:
                    selected.append(page)
            if not selected and supplement:
                # 仅对明确提出的补读疑点查相邻新页，不默认扩张初轮范围。
                for hit in hits:
                    if hit["page"] not in old_pages:
                        continue
                    for page in (hit["page"] + 1, hit["page"] - 1):
                        if (1 <= page <= paper["total_pages"] and page not in old_pages
                                and page not in selected and len(selected) < page_limit):
                            selected.append(page)
            if not selected and not supplement:
                selected = [page for page in paper["summary_pages"] if page not in old_pages][:page_limit]
            if not selected:
                if not supplement:
                    raise ValueError(f"指定论文没有可阅读的概括原文或正文定位页：{paper['filename']}")
                continue
            self._check_time(deadline)
            pages = tools.read_pdf_pages(paper["path"], sorted(selected))["pages"]
            if not any(page["text"].strip() for page in pages):
                raise ValueError(f"指定论文未提取到可阅读正文：{paper['filename']}")
            readings.append({
                "paper_id": paper["paper_id"], "filename": paper["filename"], "pages": pages,
                "page_priority": selected, "known_information": paper.get("known_information", []),
                "reading_scope": {
                    "unmatched_questions": unmatched,
                    "note": "这是按问题选取的有限页面，未展示的信息须保留缺口，不能断定整篇论文没有研究。",
                },
            })
            logger.info("C 类阅读 %s", _dump({
                "event": "pages_selected", "paper_id": paper["paper_id"], "supplement": supplement,
                "questions": len(queries), "pages": len(pages),
                "text_chars": sum(len(page["text"]) for page in pages),
            }))
        return readings

    def _fit_note_batches(self, question, outline, readings, retries, deadline):
        # 预算不足时只缩减可选页面，每篇至少保留一页，并明确记录延后页。
        prepared = [{**paper, "pages": list(paper["pages"]),
                     "page_priority": list(paper.get("page_priority", [page["page"] for page in paper["pages"]])),
                     "deferred_pages": []} for paper in readings]
        builder = lambda batch: build_messages(
            NOTES_PROMPT, question, outline=outline,
            valid_section_ids=[section["section_id"] for section in outline["sections"]], papers=batch,
        )
        if not prepared:
            return [], builder
        while True:
            units = []
            for paper in prepared:
                units.extend(self._units(paper))
            batches = self._batches(units, builder)
            available = deadline - time.monotonic() - DELIVERY_RESERVE_S - self._future_reserve("材料整理")
            needed = max(0, len(batches) - 1) * self._request_estimate(retries) + MIN_REQUEST_WINDOW_S
            if available >= needed:
                logger.info("C 类组批 %s", _dump({
                    "event": "notes_batches", "batches": len(batches),
                    "available_s": round(available, 3), "estimated_s": round(needed, 3),
                    "deferred_pages": sum(len(paper["deferred_pages"]) for paper in prepared),
                }))
                return batches, builder
            reducible = [paper for paper in prepared if len(paper["pages"]) > 1]
            if not reducible:
                raise StageBudgetError("材料整理", "budget")
            paper = max(reducible, key=lambda item: sum(len(page["text"]) for page in item["pages"]))
            removed = paper["page_priority"].pop()
            paper["pages"] = [page for page in paper["pages"] if page["page"] != removed]
            paper["deferred_pages"].append(removed)

    async def _notes(self, question, outline, readings, notes, retries, deadline):
        batches, builder = self._fit_note_batches(question, outline, readings, retries, deadline)
        added = False
        for index, batch in enumerate(batches):
            try:
                records = await self._request(
                    "材料整理", builder(batch), lambda data: _check_notes(data, batch, outline),
                    retries, deadline, len(batches) - index, recoverable=True,
                    diagnostic={"case_id": question["case_id"], "batch": index + 1,
                                "paper_ids": [paper["paper_id"] for paper in batch]},
                )
            except StageFailure as failure:
                self._merge_note_records(notes, failure.partial_records)
                batch_pages = {paper["paper_id"]: paper.get("pages", []) for paper in batch}
                for record in failure.partial_records:
                    item = notes.get(record.get("paper_id"), {})
                    for page in batch_pages.get(record.get("paper_id"), []):
                        if page.get("page") not in item.get("source_pages", []):
                            item.setdefault("source_pages", []).append(page["page"])
                accepted_ids = {record.get("paper_id") for record in failure.partial_records}
                for paper in batch:
                    if paper["paper_id"] not in accepted_ids:
                        item = notes.setdefault(paper["paper_id"], {"paper_id": paper["paper_id"],
                            "research_focus": [], "methods": [], "conditions": [], "findings": [],
                            "limitations": [], "gaps": [], "source_pages": []})
                    else:
                        item = notes[paper["paper_id"]]
                    gap = "材料整理阶段未完成；本篇已读取原文仍可用于写作核验。"
                    if gap not in item["gaps"]:
                        item["gaps"].append(gap)
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "notes",
                              "case_id": question["case_id"], "batch": index + 1,
                              "record_count": len(failure.partial_records), "reason": failure.reason}))
                return added or bool(failure.partial_records)
            by_id = {paper["paper_id"]: paper for paper in batch}
            for record in records:
                # TODO：模型提炼的事实与分析尚未做语义核验，不能标记为已验证事实。
                item = notes.setdefault(record["paper_id"], {
                    "paper_id": record["paper_id"], "research_focus": [], "methods": [], "conditions": [],
                    "findings": [], "limitations": [], "gaps": [], "source_pages": [],
                })
                if record["research_focus"] and record["research_focus"] not in item["research_focus"]:
                    item["research_focus"].append(record["research_focus"])
                for name in ("methods", "conditions", "findings", "limitations", "gaps"):
                    for value in record[name]:
                        if value not in item[name]:
                            item[name].append(value)
                added = True
                for page in by_id[record["paper_id"]]["pages"]:
                    if page["page"] not in item["source_pages"]:
                        item["source_pages"].append(page["page"])
                deferred = by_id[record["paper_id"]].get("deferred_pages", [])
                if deferred:
                    gap = "本轮因时间预算延后页面：" + "、".join(str(page) for page in deferred)
                    if gap not in item["gaps"]:
                        item["gaps"].append(gap)
        return added

    def _merge_note_records(self, notes, records):
        for record in records:
            paper_id = record.get("paper_id")
            if type(paper_id) is not int:
                continue
            item = notes.setdefault(paper_id, {"paper_id": paper_id, "research_focus": [], "methods": [],
                "conditions": [], "findings": [], "limitations": [], "gaps": [], "source_pages": []})
            focus = record.get("research_focus")
            if focus and focus not in item["research_focus"]:
                item["research_focus"].append(focus)
            for name in ("methods", "conditions", "findings", "limitations", "gaps"):
                for value in record.get(name, []):
                    if value not in item[name]:
                        item[name].append(value)

    def _has_writing_evidence(self, notes):
        for note in notes.values():
            for name in ("research_focus", "methods", "conditions", "findings", "limitations"):
                if any(bool(value) for value in note.get(name, [])):
                    return True
        return False

    async def _compare(self, question, outline, sources, notes, retries, deadline, supplement_allowed):
        context = {"outline": outline, "notes": list(notes.values()), "supplement_allowed": supplement_allowed}
        messages = build_messages(COMPARISON_PROMPT, question, **context)
        if len(_dump(messages)) + REPAIR_FEEDBACK_CHARS <= MAX_GLOBAL_CHARS:
            return await self._request(
                "跨论文比较", messages, lambda data: _check_comparison(data, sources, supplement_allowed),
                retries, deadline, 1, limit=MAX_GLOBAL_CHARS, recoverable=True,
                diagnostic={"case_id": question["case_id"], "batch": 1, "paper_ids": sorted(notes)},
            )
        # 全局材料较多时按章节比较，每节仍同时考虑所有论文，不拼接各批论文排名。
        combined = {"sections": [], "supplement_tasks": []}
        for index, section in enumerate(outline["sections"]):
            chapter_notes = []
            for note in notes.values():
                findings = [item for item in note["findings"] if not item["section_ids"] or section["section_id"] in item["section_ids"]]
                chapter_notes.append({**note, "findings": findings})
            messages = build_messages(COMPARISON_PROMPT, question,
                outline={**outline, "sections": [section]}, notes=chapter_notes, supplement_allowed=supplement_allowed)
            try:
                result = await self._request(
                    "跨论文比较", messages, lambda data: _check_comparison(data, sources, supplement_allowed),
                    retries, deadline, len(outline["sections"]) - index, limit=MAX_GLOBAL_CHARS, recoverable=True,
                    diagnostic={"case_id": question["case_id"], "batch": index + 1, "paper_ids": sorted(notes)},
                )
            except StageFailure as failure:
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery",
                              "stage": "comparison", "batch": index + 1, "record_count": len(combined["sections"]),
                              "reason": failure.reason}))
                combined["sections"].extend({"title": pending["title"], "points": [],
                                              "gaps": ["本节跨论文比较未完成"]}
                                             for pending in outline["sections"][index:])
                return combined
            combined["sections"].extend(result["sections"])
            for task in result["supplement_tasks"]:
                if task not in combined["supplement_tasks"]:
                    combined["supplement_tasks"].append(task)
        return combined

    def _outline_comparison(self, outline):
        return {"sections": [{"title": section["title"], "points": [],
                              "gaps": ["本阶段未能形成可核验的跨论文比较"]}
                             for section in outline["sections"]], "supplement_tasks": []}

    def _writing_messages(self, question, outline, comparison, sources, notes, readings):
        # 保留全部论文的结构化记录；原文按剩余空间逐页附加，不重复塞入全部 PDF。
        context = {
            "requirements": outline["requirements"], "outline": comparison["sections"],
            "notes": list(notes.values()),
            "sources": [{key: paper[key] for key in ("paper_id", "filename", "read_pages")} for paper in sources],
            "unresolved_questions": comparison.get("supplement_tasks", []),
            "pages": [],
        }
        # 从已有证据中给每篇安排可落笔的位置，不为缺失材料推断事实。
        coverage = []
        for paper in sources:
            note = notes.get(paper["paper_id"], {})
            points = [point for section in comparison["sections"] for point in section["points"]
                      if paper["paper_id"] in point["paper_ids"]]
            # 避免把长篇记录重复复制进提示；仅使用短而完整的原始论点。
            findings = [item["statement"] for item in note.get("findings", [])
                        if len(item["statement"]) <= COVERAGE_FINDING_CHARS][:1]
            coverage.append({
                "paper_id": paper["paper_id"], "filename": paper["filename"],
                "available_evidence": findings,
                "comparison_points": [item["analysis"] for item in points[:1]
                                      if len(item["analysis"]) <= COVERAGE_COMPARISON_CHARS],
                "suggested_sections": list(dict.fromkeys(
                    section["title"] for section in comparison["sections"]
                    if any(paper["paper_id"] in point["paper_ids"] for point in section["points"]))),
                "known_gaps": [value for value in note.get("gaps", [])
                               if len(value) <= COVERAGE_FINDING_CHARS][:2],
            })
        context["coverage_guidance"] = coverage
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

    def _coverage_repair_messages(self, question, sources, candidate, notes, comparison, requirements):
        # 修复请求独立限长，完整候选正文与原题并列提供，绝不追加到64k首稿输入。
        by_id = {paper["paper_id"]: paper for paper in sources}
        evidence = []
        for number in candidate.missing:
            paper = by_id[number]
            note = notes.get(number, {})
            concise = {}
            for key in ("research_focus", "methods", "conditions", "findings", "limitations", "gaps"):
                values = note.get(key, [])
                if isinstance(values, str):
                    values = [values]
                texts = [item.get("statement", "") if isinstance(item, dict) else item for item in values]
                concise[key] = [value for value in texts
                                if isinstance(value, str) and len(value) <= COVERAGE_FINDING_CHARS][:1]
            evidence.append({"paper_id": number, "filename": paper["filename"],
                             "existing_notes": concise,
                             "comparison_points": [point["analysis"] for section in comparison["sections"]
                                                   for point in section["points"] if number in point["paper_ids"]
                                                   and len(point["analysis"]) <= COVERAGE_COMPARISON_CHARS][:1],
                             "suggested_sections": [section["title"] for section in comparison["sections"]
                                                    if any(number in point["paper_ids"] for point in section["points"])],
                             "known_gaps": [value for value in note.get("gaps", [])
                                            if isinstance(value, str) and len(value) <= COVERAGE_FINDING_CHARS][:2]})
        request = build_messages(COVERAGE_REPAIR_PROMPT, question,
                                 candidate_markdown=_report_body(candidate.markdown),
                                 requirements={"min_chars": requirements["min_chars"],
                                               "min_references": candidate.min_references},
                                 cited_sources=candidate.cited, missing_sources=evidence)
        return request if len(_dump(request)) <= MAX_GLOBAL_CHARS else None

    async def handle(self, question: dict) -> dict:
        deadline = time.monotonic() + TASK_BUDGET_S
        # 耗时记录与重试余额均属于当前请求，补读不重置阶段余额。
        durations = []
        retries = {name: {"remaining": MAX_STAGE_RETRIES, "durations": durations}
                   for name in ("outline", "overview", "notes", "comparison", "writing")}

        # 一、按原题生成暂定提纲及统一阅读问题。
        outline = await self._request(
            "暂定提纲", build_messages(OUTLINE_PROMPT, question), _check_outline,
            retries["outline"], deadline, 1,
        )

        # 二、只定位本题指定论文；编号由程序固定。
        tools = PDFTools(question["materials"], self.papers_directory, self.work_directory / "pdf_cache")
        sources = tools.list_papers()
        if len(sources) < outline["requirements"]["min_references"]:
            raise ValueError("指定论文数量不足以满足引用要求，不能降低篇数或编造文献")
        summaries = []
        for number, paper in enumerate(sources, 1):
            self._check_time(deadline)
            summary = read_abstract(tools, paper)
            excerpt = self._overview_excerpt(summary)
            if not excerpt["pages"]:
                raise ValueError(f"指定论文未提取到可阅读的概括原文：{paper['filename']}")
            paper.update(paper_id=number, total_pages=summary["total_pages"],
                         summary_pages=sorted({page["page"] for page in excerpt["pages"]}), read_pages=[])
            summaries.append({**summary, "paper_id": number})

        # 三、形成每篇的阅读计划；四、围绕尚未回答的问题取证。
        overview = await self._overview(question, outline, summaries, retries["overview"], deadline)
        overview_by_id = {paper["paper_id"]: paper for paper in overview}
        for paper in sources:
            paper["known_information"] = overview_by_id.get(paper["paper_id"], {}).get("known_information", [])
        tasks = [{**query, "paper_id": paper["paper_id"]} for paper in sources
                 for query in overview_by_id.get(paper["paper_id"], {}).get("reading_questions", [])]
        for paper in sources:
            overview_record = overview_by_id.get(paper["paper_id"])
            if overview_record is None or (not overview_record.get("known_information")
                                           and not overview_record.get("reading_questions")):
                tasks.extend({**query, "paper_id": paper["paper_id"]}
                             for query in outline["reading_questions"])
        readings = self._read_tasks(tools, sources, tasks, deadline)

        # 五、逐篇材料整理；六、跨论文比较，并最多集中补读一轮。
        notes = {}
        try:
            await self._notes(question, outline, readings, notes, retries["notes"], deadline)
        except StageFailure as failure:
            self._merge_note_records(notes, failure.partial_records)
            logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "notes",
                          "case_id": question["case_id"], "record_count": len(failure.partial_records),
                          "reason": failure.reason}))
        has_notes = self._has_writing_evidence(notes)
        has_pages = any(page.get("text", "").strip() for reading in readings for page in reading.get("pages", []))
        if not has_notes and not has_pages:
            raise StageFailure("材料整理", "no_evidence")
        comparison = self._outline_comparison(outline)
        if has_notes:
            try:
                comparison = await self._compare(question, outline, sources, notes, retries["comparison"], deadline, True)
            except StageFailure as failure:
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "comparison",
                              "case_id": question["case_id"], "record_count": len(notes),
                              "reason": failure.reason}))
        for _ in range(MAX_SUPPLEMENT_ROUNDS):
            if not comparison["supplement_tasks"]:
                break
            remaining = deadline - time.monotonic() - DELIVERY_RESERVE_S
            supplement_budget = (COMPARISON_RESERVE_S + WRITING_RESERVE_S
                                 + 2 * self._request_estimate(retries["notes"]))
            if remaining < supplement_budget:
                logger.info("C 类补读 %s", _dump({"event": "supplement_deferred", "remaining_s": round(remaining, 3)}))
                break
            # 提取过但被预算延后的页仍允许重新进入材料整理。
            already_read = {paper_id: set(note["source_pages"]) for paper_id, note in notes.items()}
            try:
                extra = self._read_tasks(tools, sources, comparison["supplement_tasks"], deadline, already_read)
            except StageBudgetError:
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "supplement_read",
                              "case_id": question["case_id"], "record_count": len(comparison["sections"]),
                              "reason": "budget"}))
                break
            if not extra:
                logger.info("C 类补读 %s", _dump({"event": "supplement_no_new_evidence"}))
                break
            notes_added = False
            try:
                notes_added = await self._notes(question, outline, extra, notes, retries["notes"], deadline)
            except StageFailure as failure:
                self._merge_note_records(notes, failure.partial_records)
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "supplement_notes",
                              "case_id": question["case_id"], "record_count": len(failure.partial_records),
                              "reason": failure.reason}))
            readings.extend(extra)
            if not notes_added:
                break
            try:
                comparison = await self._compare(question, outline, sources, notes, retries["comparison"], deadline, False)
            except StageFailure as failure:
                logger.warning("C 类阶段恢复 %s", _dump({"event": "stage_recovery", "stage": "supplement_comparison",
                              "case_id": question["case_id"], "record_count": len(comparison["sections"]),
                              "reason": failure.reason}))

        # 七、写作后规范引用并校验，书目与答卷路径由程序组装。
        for paper in sources:
            shown = {page["page"] for reading in readings if reading["paper_id"] == paper["paper_id"] for page in reading["pages"]}
            paper["read_pages"] = sorted(shown.union(paper["summary_pages"]))
        markdown = await self._request(
            "综述写作", self._writing_messages(question, outline, comparison, sources, notes, readings),
            lambda text: assemble_markdown(text, sources, outline["requirements"]),
            retries["writing"], deadline, 1, json_reply=False, limit=MAX_GLOBAL_CHARS,
            coverage={"candidate": None, "question": question, "sources": sources,
                      "notes": notes, "comparison": comparison,
                      "requirements": outline["requirements"]},
        )

        logger.info("C 类答卷 %s", _dump({"event": "answer_ready", "case_id": question["case_id"],
                     "markdown_chars": len(markdown)}))

        # 八、交由 Agent 的公共交付组件原子写入及回读，再返回 HTTP 答卷。
        return {
            "case_id": question["case_id"],
            "path": f"data/expert/{question['case_id']}-综述-{self.team}.md",
            "markdown": markdown,
        }
