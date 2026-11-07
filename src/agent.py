"""业务 Agent：先核对当前派题，再调用对应题型的处理器。"""

import os
from pathlib import Path

from agenthack import BaseAgent

from src.dispatcher import Dispatcher
from src.delivery import ResultDelivery
from src.llm.client import LLMClient
from src.handlers import (
    ChatHandler, RetrievalHandler, SurveyHandler, ReproductionHandler, IdeaHandler,
)


class ResearchPilotAgent(BaseAgent):
    """接收原始题目并返回答卷字典，HTTP 与追踪由 runtime 负责。"""

    def __init__(self):
        self.dispatcher = Dispatcher()
        self.delivery = ResultDelivery(self.dispatcher.workspace_root, self.dispatcher.team)
        self.llm = LLMClient()
        papers_directory = Path(os.getenv("DATA_PATH") or "/data") / "papers"
        work_directory = self.dispatcher.team_directory / "work"
        self.handlers = {
            "Chat": ChatHandler(self.llm),
            "Literature_Retrieval": RetrievalHandler(
                self.llm,
                papers_directory=papers_directory,
                work_directory=work_directory,
            ),
            "Survey": SurveyHandler(
                self.llm,
                papers_directory=papers_directory,
                work_directory=work_directory,
                team=self.dispatcher.team,
            ),
            "Reproduction": ReproductionHandler(),
            "Idea": IdeaHandler(
                self.llm,
                papers_directory=papers_directory,
                work_directory=work_directory,
                team=self.dispatcher.team,
            ),
        }

    async def solve(self, payload: dict) -> dict:
        question = self.dispatcher.resolve(payload)
        handler = self.handlers[question["function"]]
        result = await handler.handle(question)
        # 正式文件写入并回读一致后，再交给 runtime 返回原答卷。
        return self.delivery.deliver(question, result)
