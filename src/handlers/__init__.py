"""五类处理器入口；A/B/C/E 类已实现，D 类保留接入骨干。"""

from src.handlers.chat import ChatHandler
from src.handlers.idea import IdeaHandler
from src.handlers.retrieval import RetrievalHandler
from src.handlers.survey import SurveyHandler


class ReproductionHandler:
    async def handle(self, question: dict) -> dict:
        raise NotImplementedError("Reproduction handler is not implemented")
