"""
Laplace — LLM 模块单元测试

测试适配器架构下的 chat_completion / agent_completion 降级链。
"""

import asyncio
import json as _json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import server.llm.provider as llm_provider
from server.llm import chat_completion, extract_json_object
from server.llm.adapters.dashscope_adapter import DashscopeAdapter
from server.llm.adapters.obao_adapter import ObaoAdapter
from server.llm.adapters.openai_adapter import OpenAIAdapter
from server.llm.provider import LLMProvider, agent_completion

VALID_JSON = '{"intent":"query_servants","conditions":{"npCharge":{"op":"eq","value":30}}}'


# --- 测试用适配器工厂 ---


def _make_openai_provider(name="provider_a", models=None):
    """创建带 OpenAI 适配器的测试 provider。"""
    models = models or ["model-a1", "model-a2"]
    p = LLMProvider(name=name, base_url="https://a.test/v1", api_key="key-a", models=models)
    p.adapter = OpenAIAdapter(name=name, base_url="https://a.test/v1", api_key="key-a")
    return p


def _make_obao_provider(name="obao", models=None):
    """创建带 Obao 适配器的测试 provider。"""
    models = models or ["claude-sonnet-4-6"]
    p = LLMProvider(name=name, base_url="https://api.obao.cloud/v1", api_key="key-obao", models=models)
    p.adapter = ObaoAdapter(name=name, base_url="https://api.obao.cloud/v1", api_key="key-obao")
    return p


# --- Mock 响应构造器 ---


def _make_openai_response(output_text: str):
    """构造 openai SDK responses.create 的 fake 返回对象。"""
    return SimpleNamespace(output_text=output_text, usage=None)


def _make_openai_chat_response(content: str | None = None, tool_calls: list | None = None):
    """构造 openai SDK chat.completions.create 的 fake 返回对象。"""
    tc_objs = []
    if tool_calls:
        for tc in tool_calls:
            tc_objs.append(
                SimpleNamespace(
                    id=tc.get("id", "call_1"),
                    function=SimpleNamespace(name=tc["name"], arguments=tc.get("arguments", "{}")),
                )
            )
    msg_dict = {"role": "assistant", "content": content, "tool_calls": None}
    if tc_objs:
        msg_dict["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tc_objs
        ]
    msg = SimpleNamespace(content=content, tool_calls=tc_objs or None, model_dump=lambda: msg_dict)
    choice = SimpleNamespace(message=msg)
    usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_mock_openai_client(responses_output=None, chat_output=None):
    """构造一个 mock AsyncOpenAI 客户端。"""
    client = MagicMock()
    if responses_output is not None:
        if isinstance(responses_output, list):
            client.responses.create = AsyncMock(side_effect=responses_output)
        else:
            client.responses.create = AsyncMock(return_value=responses_output)
    else:
        client.responses.create = AsyncMock(return_value=_make_openai_response(""))
    if chat_output is not None:
        if isinstance(chat_output, list):
            client.chat.completions.create = AsyncMock(side_effect=chat_output)
        else:
            client.chat.completions.create = AsyncMock(return_value=chat_output)
    else:
        client.chat.completions.create = AsyncMock(return_value=_make_openai_chat_response(content="ok"))
    return client


@pytest.fixture(autouse=True)
def setup_providers(monkeypatch):
    """设置默认测试 provider。"""
    provider = _make_openai_provider()
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])


def run(coro):
    return asyncio.run(coro)


# --- Schema / Validator ---


def _custom_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "conditions": {"type": "object"},
        },
        "required": ["intent", "conditions"],
        "additionalProperties": False,
    }


def _custom_validator_intent(content: str | dict) -> dict:
    raw = content if isinstance(content, dict) else _json.loads(extract_json_object(content))
    if "intent" not in raw:
        raise ValueError("Missing 'intent' field or invalid intent")
    if raw["intent"] != "query_servants":
        raise ValueError(f"Invalid intent: {raw['intent']}")
    return raw


# ============================================================
# OpenAI 适配器测试（Responses API）
# ============================================================


def test_chat_completion_openai_structured(monkeypatch):
    """openai provider：结构化输出（Responses API）。"""
    provider = _make_openai_provider()
    mock_client = _make_mock_openai_client(responses_output=_make_openai_response(VALID_JSON))
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            model="model-a1",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_model"] == "model-a1"
    assert result["_provider"] == "provider_a"
    assert result["_response_format"] == "json_schema"
    mock_client.responses.create.assert_called_once()


def test_chat_completion_openai_downgrades(monkeypatch):
    """openai provider：结构化输出不支持时降级为纯文本。"""
    provider = _make_openai_provider()
    mock_client = _make_mock_openai_client(
        responses_output=[
            Exception("text.format json_schema unsupported"),
            _make_openai_response(f"```json\n{VALID_JSON}\n```"),
        ]
    )
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            model="model-a1",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_response_format"] == "text_fallback"


def test_chat_completion_fallback_within_same_provider(monkeypatch):
    """同提供商内模型降级：model-a1 重试3次全部失败 → model-a2 成功。"""
    provider = _make_openai_provider()
    call_count = 0
    from server.llm import base as llm_base

    monkeypatch.setattr(llm_base, "RETRY_BACKOFF", [0, 0, 0])

    async def fake_responses_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return _make_openai_response('{"intent":"unknown","conditions":{}}')
        return _make_openai_response(VALID_JSON)

    mock_client = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=fake_responses_create)
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_model"] == "model-a2"
    assert result["_provider"] == "provider_a"


def test_chat_completion_cross_provider_fallback(monkeypatch):
    """跨提供商降级：provider_a 全部模型失败 → 自动切换 provider_b。"""
    provider_a = _make_openai_provider(name="provider_a", models=["model-a1", "model-a2"])
    provider_b = _make_openai_provider(name="provider_b", models=["model-b1"])
    from server.llm import base as llm_base

    monkeypatch.setattr(llm_base, "RETRY_BACKOFF", [0, 0, 0])

    call_count = 0
    total_fail = len(provider_a.models) * 3  # 2 models × 3 retries = 6

    async def fake_responses_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= total_fail:
            return _make_openai_response('{"intent":"unknown","conditions":{}}')
        return _make_openai_response(VALID_JSON)

    mock_client = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=fake_responses_create)
    monkeypatch.setattr(provider_a.adapter, "_client", mock_client)
    monkeypatch.setattr(provider_b.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider_a, provider_b])

    result = run(
        chat_completion(
            "system",
            "user",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_model"] == "model-b1"
    assert result["_provider"] == "provider_b"


def test_attempts_log_includes_provider_field(monkeypatch):
    """_attempts 日志包含 provider 字段。"""
    provider = _make_openai_provider()
    from server.llm import base as llm_base

    monkeypatch.setattr(llm_base, "RETRY_BACKOFF", [0, 0, 0])

    call_count = 0

    async def fake_responses_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            return _make_openai_response('{"intent":"unknown","conditions":{}}')
        return _make_openai_response(VALID_JSON)

    mock_client = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=fake_responses_create)
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert len(result["_attempts"]) == 1
    assert result["_attempts"][0]["provider"] == "provider_a"
    assert result["_attempts"][0]["model"] == "model-a1"


def test_chat_completion_requires_schema_for_json_mode():
    """json_mode=True 时不传 schema/validator 应报 ValueError。"""
    with pytest.raises(ValueError, match="json_mode=True requires"):
        run(chat_completion("system", "user", model="model-a1"))


# ============================================================
# Obao 适配器测试（Chat Completions API）
# ============================================================


def test_obao_chat_completion_uses_chat_completions(monkeypatch):
    """obao provider：chat_completion 使用 Chat Completions API 而非 Responses API。"""
    provider = _make_obao_provider()
    mock_client = _make_mock_openai_client(chat_output=_make_openai_chat_response(content=VALID_JSON))
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            model="claude-sonnet-4-6",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_model"] == "claude-sonnet-4-6"
    assert result["_provider"] == "obao"
    # 关键断言：使用 chat.completions.create 而非 responses.create
    mock_client.chat.completions.create.assert_called_once()
    mock_client.responses.create.assert_not_called()


def test_obao_chat_completion_text_mode(monkeypatch):
    """obao provider：纯文本模式也使用 Chat Completions API。"""
    provider = _make_obao_provider()
    mock_client = _make_mock_openai_client(chat_output=_make_openai_chat_response(content="这是纯文本回复"))
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            model="claude-sonnet-4-6",
            json_mode=False,
        )
    )

    assert result["text"] == "这是纯文本回复"
    mock_client.chat.completions.create.assert_called_once()


def test_obao_json_schema_fallback_to_json_object(monkeypatch):
    """obao provider：json_schema 不支持时降级为 json_object。"""
    provider = _make_obao_provider()
    from server.llm import base as llm_base

    monkeypatch.setattr(llm_base, "RETRY_BACKOFF", [0, 0, 0])

    call_count = 0

    async def fake_chat_create(**kwargs):
        nonlocal call_count
        call_count += 1
        rf = kwargs.get("response_format")
        if rf and rf.get("type") == "json_schema":
            raise Exception("response_format json_schema not supported")
        return _make_openai_chat_response(content=VALID_JSON)

    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=fake_chat_create)
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        chat_completion(
            "system",
            "user",
            model="claude-sonnet-4-6",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_response_format"] == "json_object"


# ============================================================
# Provider 加载测试
# ============================================================


def test_load_providers_backward_compatible(monkeypatch):
    """未配置 LLM_PROVIDERS 时回退旧变量。"""
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "fb1,fb2")

    from server.llm.provider import _load_providers

    providers = _load_providers()

    assert len(providers) == 1
    assert providers[0].name == "default"
    assert providers[0].base_url == "https://legacy.test/v1"
    assert providers[0].api_key == "legacy-key"
    assert providers[0].models == ["legacy-model", "fb1", "fb2"]
    assert providers[0].sdk_type == "openai"
    assert providers[0].adapter is not None


def test_load_providers_multi_provider(monkeypatch):
    """多提供商配置解析。"""
    monkeypatch.setenv("LLM_PROVIDERS", "alpha,beta")
    monkeypatch.setenv("LLM_ALPHA_URL", "https://alpha.test/v1")
    monkeypatch.setenv("LLM_ALPHA_KEY", "key-alpha")
    monkeypatch.setenv("LLM_ALPHA_MODELS", "m1,m2")
    monkeypatch.setenv("LLM_BETA_URL", "https://beta.test/v1")
    monkeypatch.setenv("LLM_BETA_KEY", "key-beta")
    monkeypatch.setenv("LLM_BETA_MODELS", "m3")

    from server.llm.provider import _load_providers

    providers = _load_providers()

    assert len(providers) == 2
    assert providers[0].name == "alpha"
    assert providers[0].models == ["m1", "m2"]
    assert providers[1].name == "beta"
    assert providers[1].models == ["m3"]


def test_load_providers_dashscope_sdk_type(monkeypatch):
    """dashscope provider 自动推断 sdk_type='dashscope'。"""
    monkeypatch.setenv("LLM_PROVIDERS", "dashscope,obao")
    monkeypatch.setenv("LLM_DASHSCOPE_URL", "https://dashscope.aliyuncs.com")
    monkeypatch.setenv("LLM_DASHSCOPE_KEY", "key-ds")
    monkeypatch.setenv("LLM_DASHSCOPE_MODELS", "qwen3.6-plus")
    monkeypatch.setenv("LLM_OBAO_URL", "https://api.obao.cloud/v1")
    monkeypatch.setenv("LLM_OBAO_KEY", "key-obao")
    monkeypatch.setenv("LLM_OBAO_MODELS", "claude-sonnet-4-6")

    from server.llm.provider import _load_providers

    providers = _load_providers()

    assert providers[0].sdk_type == "dashscope"
    assert providers[0].adapter is not None
    assert isinstance(providers[0].adapter, DashscopeAdapter)
    assert providers[1].sdk_type == "obao"
    assert isinstance(providers[1].adapter, ObaoAdapter)


# ============================================================
# agent_completion 测试
# ============================================================


def test_agent_completion_openai_with_tool_calls(monkeypatch):
    """openai provider agent_completion：返回 tool_calls。"""
    provider = _make_openai_provider()
    mock_client = _make_mock_openai_client(
        chat_output=_make_openai_chat_response(
            tool_calls=[{"name": "search_servants", "id": "call_abc", "arguments": '{"class":"saber"}'}],
        )
    )
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        agent_completion(
            messages=[{"role": "user", "content": "找saber"}],
            tools=[{"type": "function", "function": {"name": "search_servants"}}],
            model="model-a1",
        )
    )

    assert result["has_tool_call"] is True
    assert result["tool_calls"][0]["name"] == "search_servants"
    assert result["tool_calls"][0]["call_id"] == "call_abc"
    assert result["_provider"] == "provider_a"


def test_agent_completion_openai_text_reply(monkeypatch):
    """openai provider agent_completion：返回纯文本。"""
    provider = _make_openai_provider()
    mock_client = _make_mock_openai_client(chat_output=_make_openai_chat_response(content="这是回复"))
    monkeypatch.setattr(provider.adapter, "_client", mock_client)
    monkeypatch.setattr(llm_provider, "PROVIDERS", [provider])

    result = run(
        agent_completion(
            messages=[{"role": "user", "content": "你好"}],
            tools=[{"type": "function", "function": {"name": "search_servants"}}],
            model="model-a1",
        )
    )

    assert result["has_tool_call"] is False
    assert result["output_text"] == "这是回复"


# ============================================================
# extract_json_object 测试
# ============================================================


def test_extract_json_object_basic():
    assert extract_json_object('{"a":1}') == '{"a":1}'


def test_extract_json_object_wrapped_in_markdown():
    assert extract_json_object('```json\n{"a":1}\n```') == '{"a":1}'


def test_extract_json_object_empty():
    with pytest.raises(ValueError, match="empty"):
        extract_json_object("")


def test_extract_json_object_no_json():
    with pytest.raises(ValueError, match="does not contain"):
        extract_json_object("hello world")
