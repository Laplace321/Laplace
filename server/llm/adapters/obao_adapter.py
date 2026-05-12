"""
Laplace — Obao Cloud 适配器

继承 OpenAI 适配器，覆写 chat_completion 为 Chat Completions API。
obao 不支持 Responses API，但 agent_completion（Chat Completions + tools）可直接继承。
"""

import asyncio
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from server.llm.adapters.openai_adapter import OpenAIAdapter
from server.llm.base import MAX_RETRIES, RETRY_BACKOFF


class ObaoAdapter(OpenAIAdapter):
    """Obao Cloud 适配器 — Chat Completions API。

    - chat_completion: Chat Completions API (chat.completions.create)
      JSON 模式: json_schema(strict=False) → json_object → text_fallback
    - agent_completion: 继承父类（Chat Completions + tools）
    """

    # ── chat_completion: 覆写为 Chat Completions API ──

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
            return await self._chat_text_cc(client, model, system_prompt, user_message, max_tokens, temperature)

        return await self._chat_json_cc(
            client,
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            response_schema=response_schema,
            response_validator=response_validator,
        )

    async def _chat_text_cc(
        self,
        client: AsyncOpenAI,
        model: str,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        temperature: float,
    ) -> dict:
        """非 JSON 模式：纯文本调用（Chat Completions API）。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                choice = resp.choices[0]
                usage = resp.usage.model_dump() if resp.usage else {}
                return {"text": choice.message.content or "", "_model": model, "_usage": usage}
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [{self.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)
        raise last_error  # type: ignore[misc]

    async def _chat_json_cc(
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
        """JSON 模式（Chat Completions API）。

        降级链: json_schema(strict=False) → json_object → text_fallback
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        schema = response_schema() if response_schema else None

        # 降级链：json_schema → json_object → text_fallback
        if schema:
            response_format: dict[str, Any] | None = {
                "type": "json_schema",
                "json_schema": {
                    "name": "laplace_intent_response",
                    "strict": False,
                    "schema": schema,
                },
            }
            response_format_label = "json_schema"
        else:
            response_format = {"type": "json_object"}
            response_format_label = "json_object"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
                if response_format is not None:
                    kwargs["response_format"] = response_format

                resp = await client.chat.completions.create(**kwargs)
                content = resp.choices[0].message.content or ""
                parsed = response_validator(content)
                parsed["_model"] = model
                parsed["_response_format"] = response_format_label
                parsed["_usage"] = resp.usage.model_dump() if resp.usage else {}
                return parsed
            except Exception as e:
                err_str = str(e).lower()
                # json_schema 不支持 → 降级 json_object
                if (
                    response_format is not None
                    and response_format.get("type") == "json_schema"
                    and any(kw in err_str for kw in ("response_format", "json_schema", "structured", "schema"))
                ):
                    response_format = {"type": "json_object"}
                    response_format_label = "json_object"
                    print(f"  ↻ [{self.name}] json_schema 不支持，降级 json_object")
                    continue
                # json_object 不支持 → 降级 text_fallback
                if (
                    response_format is not None
                    and response_format.get("type") == "json_object"
                    and any(kw in err_str for kw in ("response_format", "json_object", "json_schema"))
                ):
                    response_format = None
                    response_format_label = "text_fallback"
                    print(f"  ↻ [{self.name}] json_object 不支持，降级 text_fallback")
                    continue
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF[attempt]
                    print(f"  ↻ [{self.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)

        raise last_error  # type: ignore[misc]
