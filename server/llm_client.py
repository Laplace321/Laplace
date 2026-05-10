"""
Laplace — LLM Client

OpenAI Responses API client with multi-provider fallback and a structured
intent contract for JSON-mode calls.

架构：
- 多提供商降级：通过 .env 扁平变量配置提供商链（LLM_PROVIDERS）
- 两层降级策略：同提供商内模型降级 → 跨提供商降级
- 向后兼容：未配置 LLM_PROVIDERS 时回退旧变量 LLM_BASE_URL / LLM_API_KEY 等

端点：OpenAI Responses API（/v1/responses）
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
        content = _extract_response_text(data)
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

    parsed = response_validator(_extract_response_text(data))
    parsed["_model"] = model
    parsed["_response_format"] = response_format
    return parsed


async def _retry_call(
    provider: LLMProvider,
    model: str,
    instructions: str,
    input_text: str,
    max_tokens: int,
    temperature: float,
    use_structured_output: bool,
    *,
    response_schema: Callable[[], dict] | None = None,
) -> dict:
    """对 _post_response 执行带 exponential backoff 的重试。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return await _post_response(
                provider.base_url,
                provider.api_key,
                model,
                instructions,
                input_text,
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


async def _post_response(
    base_url: str,
    api_key: str,
    model: str,
    instructions: str,
    input_text: str,
    max_tokens: int,
    temperature: float,
    use_structured_output: bool,
    *,
    response_schema: Callable[[], dict] | None = None,
) -> dict:
    """Send one Responses API request, with FlareSolverr proxy fallback for Cloudflare bypass."""
    url = f"{base_url}/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_text,
        "max_output_tokens": max_tokens,
        "temperature": temperature,
    }
    if use_structured_output:
        schema = response_schema()
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "laplace_intent_response",
                "strict": True,
                "schema": schema,
            }
        }

    # 尝试直接调用
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            if use_structured_output and resp.status_code in (400, 422):
                if _looks_like_response_format_error(resp):
                    raise LLMResponseFormatUnsupported(_safe_error_text(resp))
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            # 如果是 403 Forbidden,尝试使用 FlareSolverr 绕过 Cloudflare
            if e.response.status_code == 403:
                print(f"  ⚡ 检测到 403,尝试使用 FlareSolverr 绕过 Cloudflare...")
                return await _call_via_flaresolverr(url, payload, headers, use_structured_output)
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


def _extract_response_text(data: dict) -> str:
    """Extract text content from a Responses API response."""
    # Responses API 提供 output_text 辅助字段
    if "output_text" in data:
        return data["output_text"]

    # 兼容格式：从 output 数组中提取
    if "output" in data:
        for item in data["output"]:
            if item.get("type") == "message":
                content = item.get("content", [])
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            return part.get("text", "")
                elif isinstance(content, str):
                    return content

    raise ValueError("Responses API response has no text content")


def _looks_like_response_format_error(resp: httpx.Response) -> bool:
    text = _safe_error_text(resp).lower()
    markers = ("response_format", "json_schema", "structured", "schema")
    return any(marker in text for marker in markers)


def _safe_error_text(resp: httpx.Response) -> str:
    try:
        return resp.text[:1000]
    except Exception:
        return ""


async def _call_via_flaresolverr(url: str, payload: dict, headers: dict, use_structured_output: bool) -> dict:
    """通过 FlareSolverr 代理调用 API,绕过 Cloudflare 防护。"""
    import json
    
    flaresolverr_url = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")
    
    # FlareSolverr 需要将整个请求作为参数传递
    flaresolverr_payload = {
        "cmd": "request.post",
        "url": url,
        "postData": json.dumps(payload),
        "headers": {
            "Content-Type": "application/json",
            "Authorization": headers.get("Authorization", ""),
        },
        "maxTimeout": 60000,
    }
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(flaresolverr_url, json=flaresolverr_payload)
        resp.raise_for_status()
        result = resp.json()
        
        if result.get("status") != "ok":
            raise Exception(f"FlareSolverr 调用失败: {result.get('message')}")
        
        # 解析 FlareSolverr 返回的响应
        response_body = result.get("solution", {}).get("response", "")
        if not response_body:
            raise Exception("FlareSolverr 返回空响应")
        
        try:
            data = json.loads(response_body)
            return data
        except json.JSONDecodeError as e:
            raise Exception(f"FlareSolverr 返回的响应不是有效 JSON: {e}")

