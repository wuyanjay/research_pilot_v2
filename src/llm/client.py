"""通过赛事网关调用 OpenAI-compatible Chat Completions。"""

import os

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAIError

# 模型调用重试次数
MAX_RETRIES = 3


class LLMError(RuntimeError):
    """模型配置、请求或响应不可用；只保留可安全展示的错误信息。"""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class LLMTruncatedError(LLMError):
    """模型输出因 Token 上限截断，由 handler 决定后续处理。"""


class LLMClient:
    def __init__(self):
        # main.py 已统一加载配置；这里只读取环境中的推荐变量及兼容变量。
        self.base_url = os.getenv("CHALLENGE_LLM_BASE_URL") or os.getenv("PLATFORM_LLM_URL")
        self._api_key = os.getenv("CHALLENGE_LLM_API_KEY") or os.getenv("PLATFORM_API_KEY")
        self.model = (
            os.getenv("CHALLENGE_LLM_MODEL")
            or os.getenv("PLATFORM_LLM_MODEL")
            or os.getenv("AGENT_MODEL_NAME")
        )
        self.timeout_s = float(os.getenv("AGENT_LLM_TIMEOUT_S") or "900")

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        max_retries: int = MAX_RETRIES,
        timeout_s: float | None = None,
    ) -> str:
        """返回一次完整回复的文本；提示词、JSON 解析和答卷校验由 handler 负责。"""
        parameters = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            parameters["max_tokens"] = max_tokens
        # 默认保留原重试策略；B 类传 0，由业务阶段统一分配一次重试。
        for attempt in range(max_retries + 1):
            # 每次重试重新读取会话标识；不能用队名或 case_id 替代。
            session_id = os.getenv("CHALLENGE_SESSION_ID")
            parameters["extra_headers"] = {"X-Session-ID": session_id} if session_id else {}
            status_code = None
            try:
                # 延迟创建客户端以保留官方追踪；每次尝试结束后释放连接。
                async with AsyncOpenAI(
                    base_url=self.base_url,
                    api_key=self._api_key,
                    timeout=self.timeout_s if timeout_s is None else timeout_s,
                    max_retries=0,
                ) as client:
                    response = await client.chat.completions.create(**parameters)
            except APITimeoutError:
                error = "模型网关请求超时"
            except APIConnectionError:
                error = "模型网关连接失败"
            except APIStatusError as exc:
                # 408/409/425/429 和 5xx 属于瞬时错误，交给上层重试；其余立即报错。
                status_code = exc.status_code
                retryable = exc.status_code in (408, 409, 425, 429) or exc.status_code >= 500
                if not retryable:
                    raise LLMError(
                        "模型网关返回错误状态",
                        status_code=exc.status_code,
                        retryable=False,
                    ) from None
                error = f"模型网关返回可重试错误状态 {exc.status_code}"
            except (OpenAIError, ValueError):
                raise LLMError("模型网关调用失败，请检查连接与配置") from None
            else:
                if not response.choices:
                    error = "模型网关未返回候选回复"
                else:
                    choice = response.choices[0]
                    # 截断需要 handler 调整生成要求，不能当作完整答卷返回。
                    if choice.finish_reason == "length":
                        raise LLMTruncatedError("模型回复因 Token 上限被截断")
                    message = choice.message
                    if message is None:
                        error = "模型网关未返回回复消息"
                    elif message.tool_calls or getattr(message, "function_call", None):
                        error = "文本聊天接口收到了意外的工具调用"
                    elif choice.finish_reason != "stop":
                        error = "模型回复未正常结束"
                    elif not isinstance(message.content, str) or not message.content.strip():
                        error = "模型网关返回了空文本回复"
                    else:
                        # 只返回模型回复的文本，不返回完整 response 对象
                        return message.content.strip()
            
            # CancelledError 不被捕获，不触发重试；最终只报告最后一次失败。
            if attempt == max_retries:
                raise LLMError(error, status_code=status_code, retryable=True) from None
