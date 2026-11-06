"""Agent 启动入口。"""

from pathlib import Path
import os

from dotenv import dotenv_values

from src.agent import ResearchPilotAgent
from src.runtime import ResearchPilotRuntime


def main() -> ResearchPilotRuntime:
    # 入口统一加载配置，后续组件只读取进程环境变量。
    values = dotenv_values(Path(__file__).resolve().with_name(".env"))
    for name in (
        "RESEARCH_RESULT_ROOT", "RESEARCH_WORKSPACE_TEAM", "TEAM_NAME", "DATA_PATH",
        "CHALLENGE_SESSION_ID", "AGENT_LLM_TIMEOUT_S",
    ):
        if values.get(name) is not None:
            os.environ.setdefault(name, values[name])

    # 平台提供任一推荐或兼容变量时，整组优先使用平台值；否则从 .env 加载。
    for names in (
        ("CHALLENGE_LLM_BASE_URL", "PLATFORM_LLM_URL"),
        ("CHALLENGE_LLM_API_KEY", "PLATFORM_API_KEY"),
        ("CHALLENGE_LLM_MODEL", "PLATFORM_LLM_MODEL", "AGENT_MODEL_NAME"),
    ):
        if any(os.getenv(name) for name in names):
            continue
        for name in names:
            if values.get(name):
                os.environ[name] = values[name]
                break

    # OTLP 地址及遥测关联变量由平台注入，交给官方 Runtime 初始化时直接读取。
    return ResearchPilotRuntime(ResearchPilotAgent())


if __name__ == "__main__":
    main().run(host="0.0.0.0", port=8080)
