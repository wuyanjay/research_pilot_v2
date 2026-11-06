"""正式答卷落盘与回读；A/B 写 JSON，C 另存相同内容的 Markdown。"""

import json
import os
from pathlib import Path
import tempfile

from agenthack import tool

CHAT_FIELDS = {"case_id", "answer", "clarifying_question"}
RETRIEVAL_FIELDS = {"case_id", "papers"}
SURVEY_FIELDS = {"case_id", "path", "markdown"}


class ResultDelivery:
    def __init__(self, workspace_root: str | Path, team: str):
        # 复用 Dispatcher 确定的配置，不在这里再次读取环境变量或派题文件。
        self.workspace_root = Path(workspace_root).resolve()
        
        # 拒绝空值、.、..、../其他队伍、/tmp 等值
        if team in ("", ".", "..") or Path(team).name != team:
            raise ValueError("队伍名称必须是单个目录名")

        # /output/data/JTI
        self.team_directory = self.workspace_root / "data" / team
        self.team = team

    def deliver(self, question: dict, result: dict) -> dict:
        """交付成功才返回原答卷，不补字段、不包装，也不生成替代答案。"""
        if question["function"] == "Chat":
            fields = CHAT_FIELDS
            error = "A 类答卷必须只包含 case_id、answer 和 clarifying_question"
        elif question["function"] == "Literature_Retrieval":
            fields = RETRIEVAL_FIELDS
            error = "B 类答卷必须只包含 case_id 和 papers"
        elif question["function"] == "Survey":
            fields = SURVEY_FIELDS
            error = "C 类答卷必须只包含 case_id、path 和 markdown"
        else:
            raise NotImplementedError("当前题型的正式交付尚未接入")
        
        # 判断handler返回的结果是否包含了要求的字段
        if not isinstance(result, dict) or set(result) != fields:
            raise ValueError(error)
        
        case_id = question["case_id"]

        # 保证落盘的文件和handler返回结果的题号相同——————我感觉是冗余的
        if result["case_id"] != case_id:
            raise ValueError("答卷题号与核对后的题目不一致")
        
        # 文件名只取自当前题目；不能让 handler 或模型指定任意写入路径。————————感觉这段也是冗余的，但是先留着
        target = self.team_directory / f"{case_id}.json"
        if target.parent != self.team_directory:
            raise ValueError("结果文件必须位于本队目录内")
        if self.team_directory.resolve() != self.team_directory or target.is_symlink():
            raise ValueError("结果路径不能通过符号链接越界或指向符号链接文件")

        # 写入前完成序列化，拒绝非 JSON 数值和无法编码为 UTF-8 的内容。
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
        if question["function"] == "Survey":
            # 路径由题号和队名决定，只访问本队报告，不让模型指定写入位置。
            expected = f"data/expert/{case_id}-综述-{self.team}.md"
            if result["path"] != expected:
                raise ValueError("C 类 Markdown 路径必须符合题号-综述-队伍名的固定规则")
            report = self.workspace_root / expected
            if report.parent.resolve() != self.workspace_root / "data" / "expert" or report.is_symlink():
                raise ValueError("C 类报告不能通过符号链接写入其他位置")
            markdown_content = result["markdown"].encode("utf-8")
            # 每个文件单独原子替换；Markdown 回读成功后才发布结果 JSON。
            self._write_markdown(report, markdown_content)
        self._write_json(target, content, result)
        return result

    @tool(name="write_report_markdown")
    def _write_markdown(self, target: Path, content: bytes) -> None:
        """将本队综述写入正式位置，回读确认与 JSON 将使用的正文完全一致。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.stem}-", suffix=".tmp", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
            os.replace(temporary_path, target)
            if target.read_bytes() != content:
                raise ValueError("Markdown 回读内容与答卷正文不一致")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @tool(name="write_result_json")
    def _write_json(self, target: Path, content: bytes, result: dict) -> None:
        """实际写入及回读整体记录为一次文件工具操作，不逐字段记录工具。"""

        # 父目录不存在就一并创建；目录已经存在也不会因此报错。
        self.team_directory.mkdir(parents=True, exist_ok=True)

        
        temporary_path = None
        try:
            # 临时文件与正式文件在同一目录，关闭后原子替换，避免半份答卷。
            # 创建临时文件并写入
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.team_directory,
                prefix=f".{target.stem}-", suffix=".tmp", delete=False,
            ) as temporary:
                # temporary.name: /output/data/JTI/.A-99-x7k2m9.tmp"
                temporary_path = Path(temporary.name)
                # 把结果写入临时文件
                temporary.write(content)

            # 离开 with 块之后把文件的名称替换为指定的名称
            os.replace(temporary_path, target)

            # 从正式路径重新读取 JSON，解析成字典，与原答卷比较。
            actual = json.loads(target.read_bytes())
            if actual != result:
                raise ValueError("正式答卷回读内容与返回结果不一致")
            
        finally:
            # 无论成功或失败，都会进入 finally，清除这个过程中产生的临时文件
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
