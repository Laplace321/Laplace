"""
Laplace — LLM 适配器基类

定义所有供应商适配器必须实现的统一接口。
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

# Retry 配置
MAX_RETRIES = 3
RETRY_BACKOFF = [1.0, 2.0, 4.0]  # exponential backoff 秒数


class LLMResponseFormatUnsupported(Exception):
    """Raised when a model gateway rejects structured text.format."""


class BaseLLMAdapter(ABC):
    """LLM 供应商适配器基类。

    每个供应商（OpenAI、Obao、Dashscope）继承此类，
    实现各自的 API 调用协议。
    """

    def __init__(self, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        self.base_url = base_url
        self.api_key = api_key

    @abstractmethod
    async def chat_completion(
        self,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        json_mode: bool = True,
        *,
        response_schema: Callable[[], dict] | None = None,
        response_validator: Callable[[str | dict], dict] | None = None,
    ) -> dict:
        """结构化/纯文本对话调用。

        Args:
            model: 模型名称
            system_prompt: 系统指令
            user_message: 用户消息
            max_tokens: 最大 token 数
            temperature: 温度
            json_mode: True 时使用结构化输出
            response_schema: JSON Schema 生成函数（json_mode=True 时必须提供）
            response_validator: 响应校验函数（json_mode=True 时必须提供）

        Returns:
            解析后的 JSON 响应或 {"text": "..."}
        """

    @abstractmethod
    async def agent_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> dict:
        """Agent tool-use 多轮对话调用。

        Args:
            model: 模型名称
            messages: Chat Completions 格式的消息列表
            tools: Chat Completions 格式的 tools 定义
            max_tokens: 最大 token 数
            temperature: 温度

        Returns:
            {
                "output_text": str | None,
                "has_tool_call": bool,
                "tool_calls": [...],
                "raw_message": dict,
                "usage": {...},
            }
        """

    # ── 通用工具方法 ──

    async def _retry_loop(
        self,
        fn: Callable[..., Any],
        *,
        label: str = "",
    ) -> Any:
        """通用重试模板。

        Args:
            fn: async callable，接受 attempt (int) 参数，返回结果或抛异常。
                返回非 None 值视为成功。
            label: 日志标签（如 "[obao] 模型 claude-opus-4-6"）

        Returns:
            fn 的返回值

        Raises:
            最后一次异常
        """
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return await fn(attempt)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ {label} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)
        raise last_error  # type: ignore[misc]


def extract_json_object(content: str) -> str:
    """Extract the first complete JSON object from model text."""
    text = content.strip()
    if not text:
        raise ValueError("LLM returned empty content")

    start = text.find("{")
    if start == -1:
        raise ValueError("LLM response does not contain a JSON object")

    in_string = False
    escape = False
    depth = 0
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    raise ValueError("LLM response contains an incomplete JSON object")
