"""五类处理器入口；A/B/C 类已实现，D/E 类保留接入骨干。"""

from src.handlers.chat import ChatHandler
from src.handlers.retrieval import RetrievalHandler
from src.handlers.survey import SurveyHandler


class ReproductionHandler:
    async def handle(self, question: dict) -> dict:
        raise NotImplementedError("Reproduction handler is not implemented")


class IdeaHandler:
    async def handle(self, question: dict) -> dict:
        raise NotImplementedError("Idea handler is not implemented")
