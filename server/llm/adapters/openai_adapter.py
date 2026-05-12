"""
Laplace — OpenAI 兼容协议适配器

使用 Responses API 进行 chat_completion，
使用 Chat Completions API 进行 agent_completion。
"""

import asyncio
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from server.llm.base import (
    MAX_RETRIES,
    RETRY_BACKOFF,
    BaseLLMAdapter,
)


class OpenAIAdapter(BaseLLMAdapter):
    """OpenAI 官方 / 百炼 OpenAI 兼容协议适配器。

    - chat_completion: Responses API (responses.create)
    - agent_completion: Chat Completions API (chat.completions.create)
    """

    def __init__(self, name: str, base_url: str, api_key: str) -> None:
        super().__init__(name, base_url, api_key)
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        """获取或创建 AsyncOpenAI 客户端（懒初始化 + 缓存）。"""
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=30.0,
            )
        return self._client

    # ── chat_completion: Responses API ──

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
        client = self._get_client()

        if not json_mode:
            return await self._chat_text(client, model, system_prompt, user_message, max_tokens, temperature)

        return await self._chat_json(
            client,
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            response_schema=response_schema,
            response_validator=response_validator,
        )

    async def _chat_text(
        self,
        client: AsyncOpenAI,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        temperature: float,
    ) -> dict:
        """非 JSON 模式：纯文本调用（Responses API）。"""
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.responses.create(
                    model=model,
                    instructions=system_prompt,
                    input=user_message,
                    max_output_tokens=max_tokens,
                    temperature=temperature,
                )
                usage = {}
                if hasattr(resp, "usage") and resp.usage:
                    usage = {
                        "input_tokens": resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
                return {"text": resp.output_text or "", "_model": model, "_usage": usage}
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [{self.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)
        raise last_error  # type: ignore[misc]

    async def _chat_json(
        self,
        client: AsyncOpenAI,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        temperature: float,
        *,
        response_schema: Callable[[], dict] | None = None,
        response_validator: Callable[[str | dict], dict] | None = None,
    ) -> dict:
        """JSON 模式：优先 json_schema(strict=True) → 降级 text_fallback（Responses API）。"""
        schema = response_schema() if response_schema else None
        text_format: dict[str, Any] | None = None
        if schema:
            text_format = {
                "type": "json_schema",
                "name": "laplace_intent_response",
                "strict": True,
                "schema": schema,
            }
        response_format_label = "json_schema"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "instructions": system_prompt,
                    "input": user_message,
                    "max_output_tokens": max_tokens,
                    "temperature": temperature,
                }
                if text_format is not None:
                    kwargs["text"] = {"format": text_format}

                resp = await client.responses.create(**kwargs)
                content = resp.output_text or ""
                parsed = response_validator(content)
                parsed["_model"] = model
                parsed["_response_format"] = response_format_label
                usage = {}
                if hasattr(resp, "usage") and resp.usage:
                    usage = {
                        "input_tokens": resp.usage.input_tokens,
                        "output_tokens": resp.usage.output_tokens,
                        "total_tokens": resp.usage.total_tokens,
                    }
                parsed["_usage"] = usage
                return parsed
            except Exception as e:
                err_str = str(e).lower()
                # 检测结构化输出不支持的错误 → 降级
                if text_format is not None and any(
                    kw in err_str for kw in ("response_format", "json_schema", "structured", "schema", "text.format")
                ):
                    response_format_label = "text_fallback"
                    text_format = None
                    continue
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [{self.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)

        raise last_error  # type: ignore[misc]

    # ── agent_completion: Chat Completions API ──

    async def agent_completion(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> dict:
        client = self._get_client()

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                choice = resp.choices[0]
                message = choice.message
                raw_tool_calls = message.tool_calls or []
                has_tool_call = len(raw_tool_calls) > 0

                tool_calls = []
                for tc in raw_tool_calls:
                    tool_calls.append(
                        {
                            "name": tc.function.name or "",
                            "call_id": tc.id or "",
                            "arguments": tc.function.arguments or "{}",
                        }
                    )

                output_text = message.content if not has_tool_call else None

                return {
                    "output_text": output_text,
                    "has_tool_call": has_tool_call,
                    "tool_calls": tool_calls,
                    "raw_message": message.model_dump(),
                    "usage": resp.usage.model_dump() if resp.usage else {},
                }
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [agent] [{self.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)

        raise last_error  # type: ignore[misc]
