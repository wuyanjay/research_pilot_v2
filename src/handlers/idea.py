"""E 类：读取指定论文，生成带证据边界的可验证研究方案。"""

import json
import logging
from pathlib import Path
import time

from src.handlers.idea_evidence import build_digests
from src.handlers.idea_format import (
    body_limit_issues,
    format_idea,
    MIN_IDEA_BODY_CHARS,
)
from src.handlers.idea_prompt import MAX_OUTPUT_TOKENS, build_messages
from src.handlers.idea_review import idea_min_citations, review_idea_body
from src.handlers.report_text import clean_markdown
from src.llm.client import LLMClient, LLMError, LLMTruncatedError
from src.tools import PDFTools


logger = logging.getLogger(__name__)

# E 类与 C/D 同为 900 秒；交付预留 20 秒，单次模型请求最多 180 秒。
TASK_BUDGET_S = 900.0
DELIVERY_RESERVE_S = 20.0
MAX_REQUEST_TIMEOUT_S = 180.0
MAX_ATTEMPTS = 3
EVIDENCE_BUDGET_CHARS = 40_000


class IdeaHandler:
    def __init__(
        self,
        llm: LLMClient,
        *,
        papers_directory: str | Path,
        work_directory: str | Path,
        team: str,
    ):
        self.llm = llm
        self.papers_directory = Path(papers_directory)
        self.work_directory = Path(work_directory)
        self.team = team

    def _path(self, case_id: str) -> str:
        return f"data/expert/{case_id}-灵感Idea报告-{self.team}.md"

    @staticmethod
    def _material_entries(materials: list[str]) -> list[str]:
        """只把显式 PDF 路径/文件名交给 PDFTools；纯描述不能替代指定论文。"""
        entries = []
        for item in materials:
            if not isinstance(item, str):
                continue
            text = item.strip().strip("\"'")
            if not text.lower().endswith(".pdf"):
                continue
            if text.startswith("./"):
                text = text[2:]
            for prefix in ("data/papers/", "data/论文库/", "papers/", "论文库/"):
                if text.startswith(prefix):
                    text = text[len(prefix):]
                    break
            entries.append(text)
        return entries

    def _record_attempt(
        self,
        question: dict,
        attempt: int,
        raw: str | None,
        body: str | None,
        issues: list[str],
        warnings: list[str],
        accepted: bool,
    ) -> None:
        """把被拒绝的草稿留在本队工作区，绝不当成正式答卷。"""
        if issues:
            logger.warning(
                "E %s attempt=%s rejected: %s",
                question["case_id"],
                attempt,
                "；".join(issues)[:1500],
            )
        elif warnings:
            logger.info(
                "E %s attempt=%s warnings: %s",
                question["case_id"],
                attempt,
                "；".join(warnings)[:1500],
            )
        try:
            directory = self.work_directory / question["case_id"] / "idea"
            directory.mkdir(parents=True, exist_ok=True)
            payload = {
                "case_id": question["case_id"],
                "attempt": attempt,
                "accepted": accepted,
                "issues": issues,
                "warnings": warnings,
                "raw_markdown": (raw or "")[:20_000],
                "body": (body or "")[:20_000],
            }
            (directory / f"attempt-{attempt}.json").write_text(
                json.dumps(payload, ensure_ascii=True, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("E 类诊断文件写入失败：%s", question["case_id"])

    @staticmethod
    def _feedback(error: str) -> str:
        return (
            f"上一份报告不合规：{error[:800]}。"
            "请从原题和指定论文摘录重新输出完整、非空的 Markdown 正文；"
            "正文使用 [编号] 或 [编号, p. 页码] 引用实际提供的论文，"
            "不要输出参考文献列表，程序会统一重建。"
        )

    async def handle(self, question: dict) -> dict:
        deadline = time.monotonic() + TASK_BUDGET_S
        entries = self._material_entries(question["materials"])
        if not entries:
            raise ValueError(
                "E 类 materials 未提供显式 PDF 路径或文件名，不能扫描论文库替代指定材料"
            )

        tools = PDFTools(
            entries,
            self.papers_directory,
            self.work_directory / "pdf_cache",
        )
        try:
            papers = tools.list_papers()
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            raise ValueError(
                "E 类指定 PDF 无法定位，请核对 materials 中的文件名与路径"
            ) from exc
        if not papers:
            raise ValueError("E 类没有解析到可读取的指定论文")

        query = "\n".join(
            [
                question["title"],
                question["input"],
                *question["constraints"],
                question["expected_output"],
            ]
        )
        digests = build_digests(
            tools,
            papers,
            query,
            budget=EVIDENCE_BUDGET_CHARS,
            deadline=deadline - DELIVERY_RESERVE_S,
        )
        minimum = idea_min_citations(question, len(digests))
        if minimum > len(digests):
            raise ValueError(
                f"题目要求至少引用 {minimum} 篇，但本题只解析到 {len(digests)} 篇指定论文"
            )
        base_messages = build_messages(question, digests)
        messages = base_messages
        last_error = "没有生成合规报告"
        # 内容检查可能误判；保留一份格式和篇幅都合法的最佳稿，避免整题 500。
        best_markdown = None
        best_attempt = None
        best_reason = ""
        best_warnings = []

        for attempt in range(1, MAX_ATTEMPTS + 1):
            remaining = deadline - time.monotonic() - DELIVERY_RESERVE_S
            if remaining <= 0:
                break
            timeout = min(
                getattr(self.llm, "timeout_s", 900.0),
                MAX_REQUEST_TIMEOUT_S,
                remaining,
            )
            text = None
            attempt_warnings = []
            try:
                text = await self.llm.complete(
                    messages,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    temperature=0.1,
                    max_retries=0,
                    timeout_s=timeout,
                )
                body = clean_markdown(text)
                body.encode("utf-8")
                if len(body.strip()) < MIN_IDEA_BODY_CHARS:
                    raise ValueError(f"正文至少需要 {MIN_IDEA_BODY_CHARS} 个字符")
                review = review_idea_body(body, question, digests)
                citation_warnings = []
                markdown = format_idea(
                    body,
                    digests,
                    min_citations=minimum,
                    allow_unsupported_pages=True,
                    warnings=citation_warnings,
                )
                attempt_warnings = review.warnings + citation_warnings
                issues = body_limit_issues(markdown)
                if issues:
                    raise ValueError("；".join(issues))
                if review.errors:
                    best_markdown = markdown
                    best_attempt = attempt
                    best_reason = "；".join(review.errors)
                    best_warnings = attempt_warnings
                    raise ValueError(best_reason)
                self._record_attempt(
                    question, attempt, text, body, [], attempt_warnings, True
                )
                return {
                    "case_id": question["case_id"],
                    "path": self._path(question["case_id"]),
                    "markdown": markdown,
                }
            except LLMTruncatedError:
                last_error = "模型回复因 Token 上限被截断，请精简并重新输出完整 Markdown"
                self._record_attempt(
                    question, attempt, text, None, [last_error], [], False
                )
            except LLMError as exc:
                last_error = str(exc)
                self._record_attempt(
                    question, attempt, text, None, [last_error], [], False
                )
                if not exc.retryable:
                    raise
            except (ValueError, KeyError, TypeError, UnicodeEncodeError) as exc:
                last_error = str(exc)
                self._record_attempt(
                    question,
                    attempt,
                    text,
                    None,
                    [last_error],
                    attempt_warnings,
                    False,
                )

            if attempt >= MAX_ATTEMPTS:
                break
            remaining = deadline - time.monotonic() - DELIVERY_RESERVE_S
            if remaining <= 0:
                break
            messages = list(base_messages)
            if isinstance(text, str) and text.strip():
                messages.append({"role": "assistant", "content": text[:4_000]})
            messages.append({"role": "user", "content": self._feedback(last_error)})

        if best_markdown is not None:
            logger.warning(
                "E %s 使用第 %s 次最佳稿返回；内容问题：%s；警告：%s",
                question["case_id"],
                best_attempt,
                best_reason[:1000],
                "；".join(best_warnings)[:1000],
            )
            return {
                "case_id": question["case_id"],
                "path": self._path(question["case_id"]),
                "markdown": best_markdown,
            }
        raise ValueError(
            f"E 类 {question['case_id']} 报告生成失败：{last_error}"
        )
