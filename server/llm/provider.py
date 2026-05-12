"""
Laplace — LLM Provider 配置与路由调度

从 .env 解析提供商链，创建对应的适配器实例，
提供顶层 chat_completion() / agent_completion() 两层降级调度。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field

from dotenv import load_dotenv

from server.llm.base import BaseLLMAdapter

# 从项目根目录加载 .env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


@dataclass
class LLMProvider:
    """LLM 提供商配置。"""

    name: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    adapter: BaseLLMAdapter | None = None

    @property
    def sdk_type(self) -> str:
        """推断 SDK 类型，用于日志等场景。"""
        if self.name.startswith("dashscope"):
            return "dashscope"
        if self.name.startswith("obao"):
            return "obao"
        return "openai"


def _create_adapter(provider: LLMProvider) -> BaseLLMAdapter:
    """根据 provider name 创建对应的适配器实例。"""
    # 延迟 import 避免循环依赖
    from server.llm.adapters.dashscope_adapter import DashscopeAdapter
    from server.llm.adapters.obao_adapter import ObaoAdapter
    from server.llm.adapters.openai_adapter import OpenAIAdapter

    if provider.name.startswith("dashscope"):
        return DashscopeAdapter(
            name=provider.name,
            base_url=provider.base_url,
            api_key=provider.api_key,
        )
    if provider.name.startswith("obao"):
        return ObaoAdapter(
            name=provider.name,
            base_url=provider.base_url,
            api_key=provider.api_key,
        )
    # 默认 OpenAI 兼容
    return OpenAIAdapter(
        name=provider.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
    )


def _load_providers() -> list[LLMProvider]:
    """从环境变量加载 LLM 提供商链。

    支持两种格式：
    1. 新格式：LLM_PROVIDERS=dashscope,obao
       每个提供商需要：LLM_{NAME}_URL, LLM_{NAME}_KEY, LLM_{NAME}_MODELS
    2. 旧格式：LLM_BASE_URL + LLM_API_KEY + LLM_MODEL + LLM_FALLBACK_MODELS
    """
    providers_str = os.getenv("LLM_PROVIDERS", "")
    if providers_str.strip():
        providers: list[LLMProvider] = []
        for name in providers_str.split(","):
            name = name.strip()
            if not name:
                continue
            prefix = f"LLM_{name.upper()}"
            base_url = os.getenv(f"{prefix}_URL", "")
            api_key = os.getenv(f"{prefix}_KEY", "")
            models_str = os.getenv(f"{prefix}_MODELS", "")
            models = [m.strip() for m in models_str.split(",") if m.strip()]
            if not api_key:
                print(f"⚠️  提供商 {name} 缺少 API Key ({prefix}_KEY)，跳过")
                continue
            if not models:
                print(f"⚠️  提供商 {name} 未配置模型 ({prefix}_MODELS)，跳过")
                continue
            p = LLMProvider(name=name, base_url=base_url, api_key=api_key, models=models)
            p.adapter = _create_adapter(p)
            providers.append(p)
        if providers:
            return providers
        print("⚠️  LLM_PROVIDERS 已配置但无有效提供商，回退旧变量")

    # 旧格式：单提供商兼容
    base_url = os.getenv("LLM_BASE_URL", "https://api.obao.cloud/v1")
    api_key = os.getenv("LLM_API_KEY", "")
    primary = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
    fallbacks = [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",") if m.strip()]
    models = [primary] + fallbacks
    p = LLMProvider(name="default", base_url=base_url, api_key=api_key, models=models)
    p.adapter = _create_adapter(p)
    return [p]


# 模块加载时解析一次
PROVIDERS: list[LLMProvider] = _load_providers()


# ── 顶层调度函数 ──


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
    """调用 LLM，支持两层降级。

    降级策略：
    1. 同提供商内按 models 列表顺序降级
    2. 同提供商所有模型失败后，切换下一个提供商

    Args:
        system_prompt: 系统指令
        user_message: 用户消息
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
    if json_mode and (response_schema is None or response_validator is None):
        raise ValueError("json_mode=True requires both response_schema and response_validator")

    attempts_log: list[dict] = []

    for provider in PROVIDERS:
        if provider.adapter is None:
            continue
        models_to_try = [model] if model else provider.models
        for m in models_to_try:
            try:
                result = await provider.adapter.chat_completion(
                    model=m,
                    system_prompt=system_prompt,
                    user_message=user_message,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_mode=json_mode,
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


async def agent_completion(
    messages: list[dict],
    tools: list[dict],
    model: str | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> dict:
    """Agentic Tool Use 调用 — 两层降级调度。

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
        if provider.adapter is None:
            continue
        models_to_try = [model] if model else provider.models
        for m in models_to_try:
            try:
                result = await provider.adapter.agent_completion(
                    model=m,
                    messages=messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
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
