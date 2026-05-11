"""
Laplace — LLM Client

OpenAI Chat Completions API client with multi-provider fallback and a structured
intent contract for JSON-mode calls.

架构：
- 多提供商降级：通过 .env 扁平变量配置提供商链（LLM_PROVIDERS）
- 两层降级策略：同提供商内模型降级 → 跨提供商降级
- 向后兼容：未配置 LLM_PROVIDERS 时回退旧变量 LLM_BASE_URL / LLM_API_KEY 等

端点：OpenAI Chat Completions API（/v1/chat/completions）
"""

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

# 从项目根目录加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


# --- Provider 数据模型 ---


@dataclass
class LLMProvider:
    """单个 LLM 提供商配置。"""

    name: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)


def _load_providers() -> list[LLMProvider]:
    """从环境变量解析提供商链。

    优先读取新格式（LLM_PROVIDERS + LLM_{NAME}_URL/KEY/MODELS），
    未配置时回退旧格式（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL / LLM_FALLBACK_MODELS）。
    """
    providers_str = os.getenv("LLM_PROVIDERS", "").strip()

    if providers_str:
        # 新格式：多提供商配置
        providers: list[LLMProvider] = []
        for name in providers_str.split(","):
            name = name.strip().upper()
            if not name:
                continue
            base_url = os.getenv(f"LLM_{name}_URL", "").strip()
            api_key = os.getenv(f"LLM_{name}_KEY", "").strip()
            models_str = os.getenv(f"LLM_{name}_MODELS", "").strip()
            if not base_url or not api_key:
                print(f"⚠️  提供商 {name} 缺少 URL 或 KEY，已跳过")
                continue
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if not models:
                print(f"⚠️  提供商 {name} 未配置模型列表，已跳过")
                continue
            providers.append(LLMProvider(name=name.lower(), base_url=base_url, api_key=api_key, models=models))
        if providers:
            return providers
        print("⚠️  LLM_PROVIDERS 已配置但无有效提供商，回退旧变量")

    # 旧格式：单提供商兼容
    base_url = os.getenv("LLM_BASE_URL", "https://x.obao.cloud/v1")
    api_key = os.getenv("LLM_API_KEY", "")
    primary = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    fallbacks = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
    models = [primary] + fallbacks
    return [LLMProvider(name="default", base_url=base_url, api_key=api_key, models=models)]


# 模块加载时解析一次
PROVIDERS: list[LLMProvider] = _load_providers()

# Retry 配置
MAX_RETRIES = 3
RETRY_BACKOFF = [1.0, 2.0, 4.0]  # exponential backoff 秒数


class LLMResponseFormatUnsupported(Exception):
    """Raised when a model gateway rejects structured text.format."""


async def chat_completion(
    system_prompt: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.1,
    json_mode: bool = True,
    response_schema: Callable[[], dict] | None = None,
    response_validator: Callable[[str | dict], dict] | None = None,
) -> dict:
    """调用 LLM Chat Completions API，支持两层降级。

    降级策略：
    1. 同提供商内按 models 列表顺序降级
    2. 同提供商所有模型失败后，切换下一个提供商

    Args:
        system_prompt: 系统指令（Chat Completions 的 system message）
        user_message: 用户消息（Chat Completions 的 user message）
        model: 指定模型名称，None 则使用提供商链默认顺序
        max_tokens: 最大 token 数
        temperature: 温度
        json_mode: True 时使用结构化输出
        response_schema: JSON Schema 生成函数（json_mode=True 时必须提供）
        response_validator: 响应校验函数（json_mode=True 时必须提供）

    Returns:
        解析后的 JSON 响应或 {"text": "..."}

    Raises:
        Exception: 所有提供商所有模型都失败时
    """
    # json_mode=True 时，调用方必须显式传入 schema 和 validator
    if json_mode and (response_schema is None or response_validator is None):
        raise ValueError("json_mode=True requires both response_schema and response_validator")

    attempts_log: list[dict] = []

    for provider in PROVIDERS:
        # 如果指定了 model，只在第一个提供商尝试该模型
        models_to_try = [model] if model else provider.models
        for m in models_to_try:
            try:
                result = await _call_model(
                    provider,
                    m,
                    system_prompt,
                    user_message,
                    max_tokens,
                    temperature,
                    json_mode,
                    response_schema=response_schema,
                    response_validator=response_validator,
                )
                result["_provider"] = provider.name
                result["_attempts"] = attempts_log
                return result
            except Exception as e:
                attempts_log.append({"provider": provider.name, "model": m, "error": str(e)})
                print(f"⚠️  [{provider.name}] 模型 {m} 调用失败: {e}")
                continue

    raise Exception(f"所有模型都调用失败。尝试记录: {attempts_log}")


# ============================================================
# Agentic Tool Use — Chat Completions API 多轮 tool 调用
# ============================================================


async def agent_completion(
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> dict:
    """Agentic Tool Use 调用 — 使用 Chat Completions API 支持多轮 tool 调用。

    Args:
        messages: Chat Completions 格式的消息列表
            首轮: [{"role":"system",...}, {"role":"user",...}]
            多轮: [..., {"role":"assistant", "tool_calls":[...]}, {"role":"tool", ...}]
        tools: Chat Completions 格式的 tools 定义（含 function wrapper）
        model: 指定模型名称
        max_tokens: 最大 token 数
        temperature: 温度

    Returns:
        {
            "output_text": str | None,  # 文本回复（无 tool call 时）
            "has_tool_call": bool,      # 是否包含 tool_calls
            "tool_calls": [...],        # 统一格式: [{name, call_id, arguments}]
            "raw_message": dict,        # assistant 原始 message（用于构造下一轮）
            "usage": {...},             # token 用量
            "_model": str,
            "_provider": str,
        }
    """
    attempts_log: list[dict] = []

    for provider in PROVIDERS:
        models_to_try = [model] if model else provider.models
        for m in models_to_try:
            try:
                result = await _post_agent_chat(
                    provider.base_url,
                    provider.api_key,
                    m,
                    messages,
                    tools,
                    max_tokens,
                    temperature,
                )
                result["_model"] = m
                result["_provider"] = provider.name
                result["_attempts"] = attempts_log
                return result
            except Exception as e:
                attempts_log.append({"provider": provider.name, "model": m, "error": str(e)})
                print(f"⚠️  [agent] [{provider.name}] 模型 {m} 调用失败: {e}")
                continue

    raise Exception(f"[agent] 所有模型都调用失败。尝试记录: {attempts_log}")


async def _post_agent_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    temperature: float,
) -> dict:
    """发送 Chat Completions API 请求（支持多轮 tool 调用）。"""
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            # 从 Chat Completions 响应中提取
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            raw_tool_calls = message.get("tool_calls", [])
            has_tool_call = len(raw_tool_calls) > 0

            # 统一 tool_calls 格式
            tool_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {})
                tool_calls.append(
                    {
                        "name": func.get("name", ""),
                        "call_id": tc.get("id", ""),
                        "arguments": func.get("arguments", "{}"),
                    }
                )

            output_text = message.get("content") if not has_tool_call else None

            return {
                "output_text": output_text,
                "has_tool_call": has_tool_call,
                "tool_calls": tool_calls,
                "raw_message": message,
                "usage": data.get("usage", {}),
            }
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                print(f"  ↻ [agent] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                await asyncio.sleep(wait)

    raise last_error


async def _call_model(
    provider: LLMProvider,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    json_mode: bool = True,
    *,
    response_schema: Callable[[], dict] | None = None,
    response_validator: Callable[[str | dict], dict] | None = None,
) -> dict:
    """调用单个模型；JSON 模式优先尝试 text.format 结构化输出。"""
    if not json_mode:
        data = await _retry_call(
            provider,
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            use_structured_output=False,
        )
        content = _extract_chat_text(data)
        return {"text": content, "_model": model}

    response_format = "json_schema"
    try:
        data = await _retry_call(
            provider,
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            use_structured_output=True,
            response_schema=response_schema,
        )
    except LLMResponseFormatUnsupported:
        response_format = "text_fallback"
        data = await _retry_call(
            provider,
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            use_structured_output=False,
        )

    parsed = response_validator(_extract_chat_text(data))
    parsed["_model"] = model
    parsed["_response_format"] = response_format
    return parsed


async def _retry_call(
    provider: LLMProvider,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    use_structured_output: bool,
    *,
    response_schema: Callable[[], dict] | None = None,
) -> dict:
    """对 _post_chat_completion 执行带 exponential backoff 的重试。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return await _post_chat_completion(
                provider.base_url,
                provider.api_key,
                model,
                system_prompt,
                user_message,
                max_tokens,
                temperature,
                use_structured_output,
                response_schema=response_schema,
            )
        except LLMResponseFormatUnsupported:
            raise  # 格式不支持不重试，直接抛出让上层降级
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                print(f"  ↻ [{provider.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                await asyncio.sleep(wait)
    raise last_error


async def _post_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    use_structured_output: bool,
    *,
    response_schema: Callable[[], dict] | None = None,
) -> dict:
    """Send one Chat Completions API request."""
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if use_structured_output:
        schema = response_schema()
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "laplace_intent_response",
                "strict": True,
                "schema": schema,
            },
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if use_structured_output and resp.status_code in (400, 422):
                if _looks_like_response_format_error(resp):
                    raise LLMResponseFormatUnsupported(_safe_error_text(resp))
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError:
            raise


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


def _extract_chat_text(data: dict) -> str:
    """Extract text content from a Chat Completions API response."""
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if content is not None:
            return content

    raise ValueError("Chat Completions response has no text content")


def _looks_like_response_format_error(resp: httpx.Response) -> bool:
    text = _safe_error_text(resp).lower()
    markers = ("response_format", "json_schema", "structured", "schema")
    return any(marker in text for marker in markers)


def _safe_error_text(resp: httpx.Response) -> str:
    try:
        return resp.text[:1000]
    except Exception:
        return ""
