"""赛题一 HTTP 协议适配；业务校验、题型调度和文件交付由 Agent 负责。"""

import asyncio
import inspect
import logging
import os
from functools import wraps

from agenthack import AgentRuntime
from agenthack.telemetry import Telemetry, task
from fastapi import Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

TELEMETRY_FLUSH_TIMEOUT_S = 1.0
logger = logging.getLogger(__name__)


def sdk_traced(telemetry: Telemetry):
    """为异步路由建立官方追踪，并在上下文退出后尝试刷新。"""
    def decorate(route):
        # 解析原函数所在模块的注解，避免 FastAPI 将包装器参数或字符串注解误判。
        signature = inspect.signature(route, eval_str=True)

        @wraps(route)
        async def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            http_request = arguments["http_request"]
            payload = arguments["payload"]
            # 传输 ID 来自请求头，不能用题目 case_id 替代。
            task_id = http_request.headers.get("X-Research-Task-ID")
            task_type = payload.get("function")
            # 使用模型会话标识关联遥测会话，是本项目的设计选择。
            session_id = os.getenv("CHALLENGE_SESSION_ID") or None
            try:
                with telemetry.session(
                    task_id=task_id,
                    task_type=task_type,
                    session_id=session_id,
                    headers=http_request.headers,
                ):
                    with task() as operation:
                        result = await route(*args, **kwargs)
                        operation.result(result)
            finally:
                # 不限制整题耗时；只有刷新等待有上限。同步导出线程可能稍后才结束。
                # 普通刷新失败不覆盖答卷或原始异常；CancelledError 继续向外传播。
                if os.getenv("AGENTHACK_FLUSH_ON_SOLVE", "true").lower() == "true":
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(telemetry.flush),
                            timeout=TELEMETRY_FLUSH_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Telemetry flush timed out")
                    except Exception:
                        logger.warning("Telemetry flush failed")
            return result

        wrapped.__signature__ = signature
        wrapped.__annotations__ = {
            name: parameter.annotation
            for name, parameter in signature.parameters.items()
            if parameter.annotation is not inspect.Parameter.empty
        }
        if signature.return_annotation is not inspect.Signature.empty:
            wrapped.__annotations__["return"] = signature.return_annotation
        return wrapped

    return decorate


class ResearchPilotRuntime(AgentRuntime):
    """只覆盖路由；继承的初始化会创建 Telemetry、读取平台 OTLP 配置并安装拦截器。"""

    def _setup_routes(self) -> None:
        @self.app.exception_handler(RequestValidationError)
        async def invalid_request(_request: Request, _exc: RequestValidationError):
            # 不回显校验器可能携带的请求正文、字段值或内部异常。
            return JSONResponse(status_code=422, content={"detail": "invalid JSON object body"})

        @self.app.exception_handler(HTTPException)
        async def http_error(_request: Request, exc: HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": "request failed"})

        @self.app.exception_handler(Exception)
        async def server_error(_request: Request, _exc: Exception):
            return JSONResponse(status_code=500, content={"detail": "internal server error"})

        @self.app.get("/health")
        async def health():
            # 与官方 SDK 一致：报告是否已配置导出，不把它写死成 true。
            # 此标志不代表 Collector 已连通，实际任务仍须通过 SDK 导出 Trace。
            return {
                "status": "healthy",
                "telemetry_configured": self.telemetry.export_enabled,
            }

        # agent的处理解题能力入口
        @self.app.post("/solve")
        @sdk_traced(self.telemetry)
        async def solve(http_request: Request, payload: dict = Body(...)):
            # 只要求 JSON 对象，不包装、补字段或校验八字段业务内容。
            return await self.agent.solve(payload)
