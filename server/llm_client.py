"""
Laplace — LLM Client

双 SDK 多提供商 LLM 客户端，屏蔽上游供应商差异。

架构：
- 多提供商降级：通过 .env 扁平变量配置提供商链（LLM_PROVIDERS）
- 两层降级策略：同提供商内模型降级 → 跨提供商降级
- 向后兼容：未配置 LLM_PROVIDERS 时回退旧变量 LLM_BASE_URL / LLM_API_KEY 等

SDK 分派：
- dashscope provider → dashscope 官方 SDK（Generation.call，同步，asyncio.to_thread 包装）
- 其他 OpenAI 兼容 provider → openai 官方 SDK（AsyncOpenAI，原生异步）
  - chat_completion → client.responses.create（Responses API 协议）
  - agent_completion → client.chat.completions.create（Chat Completions 协议）
"""

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

from dashscope import Generation
from dotenv import load_dotenv
from openai import AsyncOpenAI

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
    sdk_type: str = "openai"  # "dashscope" | "openai"


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
            sdk_type = "dashscope" if name.lower() == "dashscope" else "openai"
            providers.append(
                LLMProvider(name=name.lower(), base_url=base_url, api_key=api_key, models=models, sdk_type=sdk_type)
            )
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

# OpenAI 客户端缓存（按 provider name 缓存，避免重复创建）
_openai_clients: dict[str, AsyncOpenAI] = {}


def _get_openai_client(provider: LLMProvider) -> AsyncOpenAI:
    """获取或创建 OpenAI 兼容 provider 的 AsyncOpenAI 客户端。"""
    if provider.name not in _openai_clients:
        _openai_clients[provider.name] = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            timeout=30.0,
        )
    return _openai_clients[provider.name]


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
    """调用 LLM Responses API，支持两层降级。

    降级策略：
    1. 同提供商内按 models 列表顺序降级
    2. 同提供商所有模型失败后，切换下一个提供商

    Args:
        system_prompt: 系统指令（Responses API 的 instructions）
        user_message: 用户消息（Responses API 的 input）
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
    """调用单个模型；根据 sdk_type 分派到对应 SDK。"""

    if provider.sdk_type == "dashscope":
        return await _call_dashscope_model(
            api_key=provider.api_key,
            model=model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            response_schema=response_schema,
            response_validator=response_validator,
        )

    # ── openai 兼容 provider → openai SDK (Responses API) ──
    return await _call_openai_model(
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        user_message=user_message,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
        response_schema=response_schema,
        response_validator=response_validator,
    )


# ============================================================
# openai SDK 适配器（Responses API / Chat Completions API）
# ============================================================


async def _call_openai_model(
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
    """openai 兼容 provider 的 chat_completion 实现（Responses API）。"""
    client = _get_openai_client(provider)

    if not json_mode:
        # 非 JSON 模式：纯文本调用
        last_error = None
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
                    print(f"  ↻ [{provider.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                    await asyncio.sleep(wait)
        raise last_error  # type: ignore[misc]

    # JSON 模式：优先尝试结构化输出
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

    last_error = None
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
                print(f"  ↻ [{provider.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                await asyncio.sleep(wait)

    raise last_error  # type: ignore[misc]


async def _openai_agent_chat(
    provider: LLMProvider,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    temperature: float,
) -> dict:
    """openai 兼容 provider 的 agent_completion 实现（Chat Completions API）。"""
    client = _get_openai_client(provider)

    last_error = None
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
                print(f"  ↻ [agent] [{provider.name}] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                await asyncio.sleep(wait)

    raise last_error  # type: ignore[misc]


async def _dashscope_agent_chat(
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    temperature: float,
) -> dict:
    """dashscope provider 的 agent_completion 实现（SDK 调用）。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await asyncio.to_thread(
                _dashscope_call_sync,
                api_key,
                model,
                messages,
                max_tokens,
                temperature,
                None,
                tools,
            )
            choice_msg = resp.output.choices[0].message
            # dashscope Message 是类 dict 对象，__getattr__ 在 key 不存在时
            # 抛出 KeyError（而非 AttributeError），getattr() 无法兜底，必须用 .get()
            content = choice_msg.get("content", None)
            raw_tool_calls = choice_msg.get("tool_calls", None) or []
            has_tool_call = len(raw_tool_calls) > 0

            tool_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", {})
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                func_name = func.get("name", "") if isinstance(func, dict) else getattr(func, "name", "")
                func_args = func.get("arguments", "{}") if isinstance(func, dict) else getattr(func, "arguments", "{}")
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


# ============================================================
# dashscope 官方 SDK 调用（Chat Completions 协议）
# ============================================================


def _dashscope_call_sync(
    api_key: str,
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    response_format: dict | None = None,
    tools: list[dict] | None = None,
) -> dict:
    """同步调用 dashscope Generation.call()，返回原始响应字典。

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
        # 检测结构化输出不支持的错误
        if error_code and any(kw in str(error_code).lower() for kw in ("response_format", "json_schema", "schema")):
            raise LLMResponseFormatUnsupported(error_msg)
        raise Exception(f"dashscope API 错误 [{error_code}]: {error_msg}")

    return response


async def _call_dashscope_model(
    api_key: str,
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
    """dashscope provider 的 chat_completion 实现（SDK 调用）。"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    if not json_mode:
        # 非 JSON 模式：纯文本调用
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = await asyncio.to_thread(
                    _dashscope_call_sync,
                    api_key,
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
        raise last_error

    # JSON 模式：dashscope SDK 仅支持 json_object，不支持 json_schema
    rf = {"type": "json_object"}
    response_format_label = "json_object"

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = await asyncio.to_thread(
                _dashscope_call_sync,
                api_key,
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
            # 不算重试，立即重新尝试（无 response_format）
            continue
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                print(f"  ↻ [dashscope] 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                await asyncio.sleep(wait)

    raise last_error


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
    """Agentic Tool Use 调用 — 根据 sdk_type 分派到对应 SDK。

    Args:
        messages: Chat Completions 格式的消息列表
        tools: Chat Completions 格式的 tools 定义
        model: 指定模型名称
        max_tokens: 最大 token 数
        temperature: 温度

    Returns:
        {
            "output_text": str | None,
            "has_tool_call": bool,
            "tool_calls": [...],
            "raw_message": dict,
            "usage": {...},
            "_model": str,
            "_provider": str,
        }
    """
    attempts_log: list[dict] = []

    for provider in PROVIDERS:
        models_to_try = [model] if model else provider.models
        for m in models_to_try:
            try:
                if provider.sdk_type == "dashscope":
                    result = await _dashscope_agent_chat(
                        provider.api_key,
                        m,
                        messages,
                        tools,
                        max_tokens,
                        temperature,
                    )
                else:
                    result = await _openai_agent_chat(
                        provider,
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
