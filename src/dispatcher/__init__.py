"""读取本队派题记录并核对当前题目，不读取材料或处理答卷。"""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path

QUESTION_FIELDS = (
    "case_id", "function", "title", "input", "materials", "constraints",
    "expected_output", "output_schema_ref",
)
CATEGORY_COUNTS = {"A": 1, "B": 3, "C": 2, "D": 3, "E": 2}


class Dispatcher:
    def __init__(self, workspace_root: str | Path | None = None, team: str | None = None):
        # 本地可显式传入根目录和队伍名；工作区不从 DATA_PATH 推导。
        if workspace_root is None:
            workspace_root = os.getenv("RESEARCH_RESULT_ROOT") or "/output"
        self.workspace_root = Path(workspace_root).resolve()
        self.team = team if team is not None else (
            os.getenv("RESEARCH_WORKSPACE_TEAM") or os.getenv("TEAM_NAME") or "JTI"
        )
        if self.team in ("", ".", "..") or Path(self.team).name != self.team:
            raise ValueError("队伍名称必须是单个目录名")
        '''
        self.team_directory  = /output/data/JTI
        '''
        self.team_directory = self.workspace_root / "data" / self.team

    def resolve(self, payload: dict) -> dict:
        """仅核对当前题，文件读取和 JSON 解析异常自然向上传播。"""
        case_id = payload["case_id"]
        dispatch_path = self.team_directory / "dispatch.json"
        if dispatch_path.resolve().parent != self.team_directory:
            raise ValueError("派题文件必须位于本队目录内")
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        if dispatch["team"] != self.team:
            raise ValueError("派题记录中的队伍与配置的队伍不一致")

        # 核对本轮派题集合即可，不扩展版本兼容或其他派题元数据校验。
        case_ids = dispatch["case_ids"]
        if len(case_ids) != 11 or len(set(case_ids)) != 11:
            raise ValueError("派题记录必须包含 11 个不重复的题号")
        counts = Counter(item.split("-", 1)[0] for item in case_ids)
        if counts != CATEGORY_COUNTS:
            raise ValueError("派题记录中 A/B/C/D/E 类题目数量必须分别为 1/3/2/3/2")
        cases = dispatch["cases"]
        entry_ids = [item["case_id"] for item in cases]
        if len(entry_ids) != 11 or set(entry_ids) != set(case_ids):
            raise ValueError("派题记录中的 cases 条目必须与 case_ids 题号一一对应")
        if case_id not in case_ids:
            raise ValueError("请求的题目不在本队派题记录中")
        entry = next(item for item in cases if item["case_id"] == case_id)

        # question_file 相对工作区根目录，而非相对 dispatch.json 所在目录。
        relative_path = Path(entry["question_file"])
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("question_file 必须是相对工作区根目录的路径，且不能包含 '..'")
        question_path = (self.workspace_root / relative_path).resolve()
        assigned_directory = self.team_directory / "assigned"
        if (question_path.parent != assigned_directory
                or question_path.name != f"{case_id}.json"):
            # resolve 后再比较，防止符号链接将题目读取引向其他队伍或目录。
            raise ValueError("question_file 必须指向本队 assigned 目录下与当前题号同名的 JSON 文件")

        # 当前题只读一次；摘要以原始字节为准，随后解析后比较字段值。
        raw_question = question_path.read_bytes()
        if hashlib.sha256(raw_question).hexdigest() != entry["question_sha256"]:
            raise ValueError("题目文件的 SHA-256 摘要与派题记录不一致")
        question = json.loads(raw_question)
        for field in ("case_id", "function", "title"):
            if question[field] != entry[field]:
                raise ValueError(f"题目文件中的 {field} 与派题记录不一致")
        if any(payload[field] != question[field] for field in QUESTION_FIELDS):
            raise ValueError("请求体中的八字段内容与派发的题目不一致")

        # 不合并输入，也不读取或改写 materials；完整题目直接交给业务 handler。
        return question
