"""
Laplace — 百炼 Dashscope 原生 SDK 适配器

使用 dashscope.Generation.call() 同步 SDK + asyncio.to_thread 包装为异步。
"""

import asyncio
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from dashscope import Generation

from server.llm.base import (
    MAX_RETRIES,
    RETRY_BACKOFF,
    BaseLLMAdapter,
    LLMResponseFormatUnsupported,
)


class DashscopeAdapter(BaseLLMAdapter):
    """百炼原生 SDK 适配器。

    - chat_completion: Generation.call 同步 + asyncio.to_thread
    - agent_completion: Generation.call(tools=...) 同步 + asyncio.to_thread
    """

    # ── 同步底层调用 ──

    @staticmethod
    def _call_sync(
        api_key: str,
        model: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
    ) -> Any:
        """同步调用 dashscope Generation.call()，返回原始响应。

        dashscope SDK 仅提供同步接口，由调用方通过 asyncio.to_thread() 包装。
        """
        kwargs: dict[str, Any] = {
            "result_format": "message",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "enable_thinking": False,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = Generation.call(
            model=model,
            api_key=api_key,
            messages=messages,
            **kwargs,
        )

        # 检查返回状态
        if response.status_code != HTTPStatus.OK:
            error_msg = getattr(response, "message", "") or f"HTTP {response.status_code}"
            error_code = getattr(response, "code", "")
            if error_code and any(kw in str(error_code).lower() for kw in ("response_format", "json_schema", "schema")):
                raise LLMResponseFormatUnsupported(error_msg)
            raise Exception(f"dashscope API 错误 [{error_code}]: {error_msg}")

        return response

    # ── chat_completion ──

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
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        if not json_mode:
            return await self._chat_text(model, messages, max_tokens, temperature)

        return await self._chat_json(
            model,
            messages,
            max_tokens,
            temperature,
            response_validator=response_validator,
        )

    async def _chat_text(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> dict:
        """非 JSON 模式：纯文本调用。"""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await asyncio.to_thread(
                    self._call_sync,
                    self.api_key,
                    model,
                    messages,
                    max_tokens,
                    temperature,
                )
                content = resp.output.choices[0].message.content
                if isinstance(content, list):
                    content = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                usage = dict(resp.usage) if hasattr(resp, "usage") and resp.usage else {}
                return {"text": content or "", "_model": model, "_usage": usage}
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [dashscope] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)
        raise last_error  # type: ignore[misc]

    async def _chat_json(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        *,
        response_validator: Callable[[str | dict], dict] | None = None,
    ) -> dict:
        """JSON 模式：json_object → text_fallback（SDK 不支持 json_schema）。"""
        rf: dict | None = {"type": "json_object"}
        response_format_label = "json_object"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await asyncio.to_thread(
                    self._call_sync,
                    self.api_key,
                    model,
                    messages,
                    max_tokens,
                    temperature,
                    response_format=rf,
                )
                content = resp.output.choices[0].message.content
                if isinstance(content, list):
                    content = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                parsed = response_validator(content)
                parsed["_model"] = model
                parsed["_response_format"] = response_format_label
                parsed["_usage"] = dict(resp.usage) if hasattr(resp, "usage") and resp.usage else {}
                return parsed
            except LLMResponseFormatUnsupported:
                # 结构化输出不支持 → 降级为纯文本
                response_format_label = "text_fallback"
                rf = None
                continue
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [dashscope] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)

        raise last_error  # type: ignore[misc]

    # ── agent_completion ──

    async def agent_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> dict:
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await asyncio.to_thread(
                    self._call_sync,
                    self.api_key,
                    model,
                    messages,
                    max_tokens,
                    temperature,
                    None,  # response_format
                    tools,
                )
                choice_msg = resp.output.choices[0].message
                content = choice_msg.get("content", None)
                raw_tool_calls = choice_msg.get("tool_calls", None) or []
                has_tool_call = len(raw_tool_calls) > 0

                tool_calls = []
                for tc in raw_tool_calls:
                    func = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    func_name = func.get("name", "") if isinstance(func, dict) else getattr(func, "name", "")
                    func_args = (
                        func.get("arguments", "{}") if isinstance(func, dict) else getattr(func, "arguments", "{}")
                    )
                    tool_calls.append(
                        {
                            "name": func_name,
                            "call_id": tc_id,
                            "arguments": func_args,
                        }
                    )

                # content 处理
                if isinstance(content, list):
                    content = content[0].get("text", "") if isinstance(content[0], dict) else str(content[0])
                output_text = content if not has_tool_call else None

                return {
                    "output_text": output_text,
                    "has_tool_call": has_tool_call,
                    "tool_calls": tool_calls,
                    "raw_message": dict(choice_msg) if hasattr(choice_msg, "items") else {},
                    "usage": dict(resp.usage) if hasattr(resp, "usage") and resp.usage else {},
                }
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [agent] [dashscope] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)

        raise last_error  # type: ignore[misc]
