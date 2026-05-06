"""
Laplace — LLM Client

OpenAI Responses API client with fallback models and a structured
intent contract for JSON-mode calls.

迁移说明：
- 从 Chat Completions API 迁移至 Responses API（2025 推荐）
- 端点：/v1/chat/completions → /v1/responses
- 参数：messages → input, system role → instructions
- 结构化输出：response_format → text.format
"""

import asyncio
import json
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from server.schemas import IntentResponse, intent_response_json_schema

# 从项目根目录加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE_URL = os.getenv("LLM_BASE_URL", "https://x.obao.cloud/v1")
API_KEY = os.getenv("LLM_API_KEY", "")
PRIMARY_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv("LLM_FALLBACK_MODELS", "Deepseek-V4-Flash,gpt-5.4").split(",")
    if m.strip()
]


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
) -> dict:
    """
    调用 LLM Responses API。

    Args:
        system_prompt: 系统指令（对应 Responses API 的 instructions）
        user_message: 用户消息（对应 Responses API 的 input）
        model: 模型名称，None 则使用主模型
        max_tokens: 最大 token 数
        temperature: 温度，低温 = 更确定性
        json_mode: True 时使用结构化输出（text.format）

    Returns:
        解析后的 JSON 响应或 {"text": "..."}

    Raises:
        Exception: 所有模型都失败时
    """
    models_to_try = [model or PRIMARY_MODEL] + FALLBACK_MODELS
    attempts_log: list[dict] = []

    last_error = None
    for m in models_to_try:
        try:
            result = await _call_model(
                m,
                system_prompt,
                user_message,
                max_tokens,
                temperature,
                json_mode,
            )
            result["_attempts"] = attempts_log
            return result
        except Exception as e:
            attempts_log.append({"model": m, "error": str(e)})
            print(f"⚠️  模型 {m} 调用失败: {e}")
            last_error = e
            continue

    raise Exception(f"所有模型都调用失败。尝试记录: {attempts_log}")


async def _call_model(
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
    json_mode: bool = True,
) -> dict:
    """调用单个模型；JSON 模式优先尝试 text.format 结构化输出。"""
    if not json_mode:
        data = await _retry_call(
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
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            use_structured_output=True,
        )
    except LLMResponseFormatUnsupported:
        response_format = "text_fallback"
        data = await _retry_call(
            model,
            system_prompt,
            user_message,
            max_tokens,
            temperature,
            use_structured_output=False,
        )

    parsed = parse_intent_response(_extract_response_text(data))
    parsed["_model"] = model
    parsed["_response_format"] = response_format
    return parsed


async def _retry_call(
    model: str,
    instructions: str,
    input_text: str,
    max_tokens: int,
    temperature: float,
    use_structured_output: bool,
) -> dict:
    """对 _post_response 执行带 exponential backoff 的重试。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return await _post_response(
                model, instructions, input_text,
                max_tokens, temperature, use_structured_output,
            )
        except LLMResponseFormatUnsupported:
            raise  # 格式不支持不重试，直接抛出让上层降级
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF[attempt]
                print(f"  ↻ 模型 {model} 第 {attempt + 1} 次失败，{wait}s 后重试: {e}")
                await asyncio.sleep(wait)
    raise last_error


async def _post_response(
    model: str,
    instructions: str,
    input_text: str,
    max_tokens: int,
    temperature: float,
    use_structured_output: bool,
) -> dict:
    """Send one Responses API request."""
    url = f"{BASE_URL}/responses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_text,
        "max_output_tokens": max_tokens,
        "temperature": temperature,
    }
    if use_structured_output:
        # Responses API 使用 text.format 而非 response_format
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "laplace_intent_response",
                "strict": True,
                "schema": intent_response_json_schema(),
            }
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if use_structured_output and resp.status_code in (400, 422):
            if _looks_like_response_format_error(resp):
                raise LLMResponseFormatUnsupported(_safe_error_text(resp))
        resp.raise_for_status()
        return resp.json()


def parse_intent_response(content: str | dict) -> dict:
    """Parse and validate an LLM intent response."""
    raw = content if isinstance(content, dict) else json.loads(extract_json_object(content))
    try:
        parsed = IntentResponse.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"LLM JSON schema validation failed: {e}") from e
    return parsed.model_dump(exclude_none=True)


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
