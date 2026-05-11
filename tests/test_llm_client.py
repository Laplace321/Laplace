import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import server.llm_client as llm_client
from server.llm_client import LLMProvider

VALID_JSON = '{"intent":"query_servants","conditions":{"npCharge":{"op":"eq","value":30}}}'

# --- 测试用提供商 ---
PROVIDER_A = LLMProvider(
    name="provider_a", base_url="https://a.test/v1", api_key="key-a", models=["model-a1", "model-a2"], sdk_type="openai"
)
PROVIDER_B = LLMProvider(
    name="provider_b", base_url="https://b.test/v1", api_key="key-b", models=["model-b1"], sdk_type="openai"
)
PROVIDER_DS = LLMProvider(
    name="dashscope",
    base_url="https://dashscope.aliyuncs.com",
    api_key="key-ds",
    models=["qwen3.6-plus"],
    sdk_type="dashscope",
)


def _make_openai_response(output_text: str):
    """构造 openai SDK responses.create 的 fake 返回对象。"""
    return SimpleNamespace(output_text=output_text)


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


@pytest.fixture(autouse=True)
def setup_providers(monkeypatch):
    """设置默认测试 provider 并清除 openai 客户端缓存。"""
    monkeypatch.setattr(llm_client, "PROVIDERS", [PROVIDER_A])
    llm_client._openai_clients.clear()


def run(coro):
    return asyncio.run(coro)


def _custom_validator_intent(content: str | dict) -> dict:
    """通用校验函数：直接解析 JSON，不做 Pydantic 校验。"""
    import json as _json

    raw = content if isinstance(content, dict) else _json.loads(llm_client.extract_json_object(content))
    if "intent" not in raw:
        raise ValueError("Missing 'intent' field or invalid intent")
    if raw["intent"] != "query_servants":
        raise ValueError(f"Invalid intent: {raw['intent']}")
    return raw


def _make_mock_openai_client(responses_output=None, chat_output=None):
    """构造一个 mock AsyncOpenAI 客户端。"""
    client = MagicMock()
    # mock responses.create
    if responses_output is not None:
        if isinstance(responses_output, list):
            client.responses.create = AsyncMock(side_effect=responses_output)
        else:
            client.responses.create = AsyncMock(return_value=responses_output)
    else:
        client.responses.create = AsyncMock(return_value=_make_openai_response(""))
    # mock chat.completions.create
    if chat_output is not None:
        if isinstance(chat_output, list):
            client.chat.completions.create = AsyncMock(side_effect=chat_output)
        else:
            client.chat.completions.create = AsyncMock(return_value=chat_output)
    else:
        client.chat.completions.create = AsyncMock(return_value=_make_openai_chat_response(content="ok"))
    return client


def test_chat_completion_openai_structured(monkeypatch):
    """openai provider：结构化输出（Responses API）。"""
    mock_client = _make_mock_openai_client(responses_output=_make_openai_response(VALID_JSON))
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)

    result = run(
        llm_client.chat_completion(
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
    call_kwargs = mock_client.responses.create.call_args
    assert call_kwargs.kwargs["instructions"] == "system"
    assert call_kwargs.kwargs["input"] == "user"
    assert "text" in call_kwargs.kwargs


def test_chat_completion_openai_downgrades(monkeypatch):
    """openai provider：结构化输出不支持时降级为纯文本。"""
    mock_client = _make_mock_openai_client(
        responses_output=[
            Exception("text.format json_schema unsupported"),
            _make_openai_response(f"```json\n{VALID_JSON}\n```"),
        ]
    )
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)

    result = run(
        llm_client.chat_completion(
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
    call_count = 0
    # model-a1 的 3 次重试都返回无效 JSON，model-a2 返回有效 JSON
    retries = llm_client.MAX_RETRIES

    async def fake_responses_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= retries:
            return _make_openai_response('{"intent":"unknown","conditions":{}}')
        return _make_openai_response(VALID_JSON)

    mock_client = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=fake_responses_create)
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)
    monkeypatch.setattr(llm_client, "RETRY_BACKOFF", [0, 0, 0])  # 加速测试

    result = run(
        llm_client.chat_completion(
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
    monkeypatch.setattr(llm_client, "PROVIDERS", [PROVIDER_A, PROVIDER_B])
    retries = llm_client.MAX_RETRIES
    call_count = 0
    # provider_a: model-a1 (3次) + model-a2 (3次) = 6次无效，provider_b: model-b1 有效
    total_fail = len(PROVIDER_A.models) * retries

    async def fake_responses_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= total_fail:
            return _make_openai_response('{"intent":"unknown","conditions":{}}')
        return _make_openai_response(VALID_JSON)

    mock_client = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=fake_responses_create)
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)
    monkeypatch.setattr(llm_client, "RETRY_BACKOFF", [0, 0, 0])

    result = run(
        llm_client.chat_completion(
            "system",
            "user",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert result["_model"] == "model-b1"
    assert result["_provider"] == "provider_b"


def test_attempts_log_includes_provider_field(monkeypatch):
    """_attempts 日志包含 provider 字段（model-a1 重试全部失败后记录）。"""
    retries = llm_client.MAX_RETRIES
    call_count = 0

    async def fake_responses_create(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= retries:
            return _make_openai_response('{"intent":"unknown","conditions":{}}')
        return _make_openai_response(VALID_JSON)

    mock_client = MagicMock()
    mock_client.responses.create = AsyncMock(side_effect=fake_responses_create)
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)
    monkeypatch.setattr(llm_client, "RETRY_BACKOFF", [0, 0, 0])

    result = run(
        llm_client.chat_completion(
            "system",
            "user",
            response_schema=_custom_schema,
            response_validator=_custom_validator_intent,
        )
    )

    assert len(result["_attempts"]) == 1
    assert result["_attempts"][0]["provider"] == "provider_a"
    assert result["_attempts"][0]["model"] == "model-a1"
    assert "error" in result["_attempts"][0]


def test_chat_completion_requires_schema_for_json_mode():
    """json_mode=True 时不传 schema/validator 应报 ValueError。"""
    with pytest.raises(ValueError, match="json_mode=True requires"):
        run(llm_client.chat_completion("system", "user", model="model-a1"))


def test_load_providers_backward_compatible(monkeypatch):
    """未配置 LLM_PROVIDERS 时回退旧变量。"""
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://legacy.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("LLM_MODEL", "legacy-model")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "fb1,fb2")

    providers = llm_client._load_providers()

    assert len(providers) == 1
    assert providers[0].name == "default"
    assert providers[0].base_url == "https://legacy.test/v1"
    assert providers[0].api_key == "legacy-key"
    assert providers[0].models == ["legacy-model", "fb1", "fb2"]
    assert providers[0].sdk_type == "openai"  # default 走 openai SDK


def test_load_providers_multi_provider(monkeypatch):
    """多提供商配置解析。"""
    monkeypatch.setenv("LLM_PROVIDERS", "alpha,beta")
    monkeypatch.setenv("LLM_ALPHA_URL", "https://alpha.test/v1")
    monkeypatch.setenv("LLM_ALPHA_KEY", "key-alpha")
    monkeypatch.setenv("LLM_ALPHA_MODELS", "m1,m2")
    monkeypatch.setenv("LLM_BETA_URL", "https://beta.test/v1")
    monkeypatch.setenv("LLM_BETA_KEY", "key-beta")
    monkeypatch.setenv("LLM_BETA_MODELS", "m3")

    providers = llm_client._load_providers()

    assert len(providers) == 2
    assert providers[0].name == "alpha"
    assert providers[0].models == ["m1", "m2"]
    assert providers[0].sdk_type == "openai"
    assert providers[1].name == "beta"
    assert providers[1].models == ["m3"]


def test_load_providers_dashscope_sdk_type(monkeypatch):
    """dashscope provider 自动推断 sdk_type='dashscope'。"""
    monkeypatch.setenv("LLM_PROVIDERS", "dashscope,obao")
    monkeypatch.setenv("LLM_DASHSCOPE_URL", "https://dashscope.aliyuncs.com")
    monkeypatch.setenv("LLM_DASHSCOPE_KEY", "key-ds")
    monkeypatch.setenv("LLM_DASHSCOPE_MODELS", "qwen3.6-plus")
    monkeypatch.setenv("LLM_OBAO_URL", "https://x.obao.cloud/v1")
    monkeypatch.setenv("LLM_OBAO_KEY", "key-obao")
    monkeypatch.setenv("LLM_OBAO_MODELS", "claude-sonnet-4-6")

    providers = llm_client._load_providers()

    assert providers[0].sdk_type == "dashscope"
    assert providers[1].sdk_type == "openai"


# --- 自定义 response_schema / response_validator 测试 ---

CUSTOM_JSON = '{"action":"search","query":"saber"}'


def _custom_schema() -> dict:
    """自定义 JSON Schema（简单对象）。"""
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "query": {"type": "string"},
        },
        "required": ["action", "query"],
        "additionalProperties": False,
    }


def _custom_validator(content: str | dict) -> dict:
    """自定义校验函数：直接解析 JSON，不做 Pydantic 校验。"""
    import json

    raw = content if isinstance(content, dict) else json.loads(llm_client.extract_json_object(content))
    if "action" not in raw:
        raise ValueError("Missing 'action' field")
    return raw


def test_custom_validator_is_used_for_parsing(monkeypatch):
    """自定义 response_validator 正确解析响应。"""
    mock_client = _make_mock_openai_client(responses_output=_make_openai_response(CUSTOM_JSON))
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)

    result = run(
        llm_client.chat_completion(
            "system",
            "user",
            model="model-a1",
            response_schema=_custom_schema,
            response_validator=_custom_validator,
        )
    )

    assert result["action"] == "search"
    assert result["query"] == "saber"
    assert result["_model"] == "model-a1"


# --- agent_completion 测试 ---


def test_agent_completion_openai_with_tool_calls(monkeypatch):
    """openai provider agent_completion：返回 tool_calls。"""
    mock_client = _make_mock_openai_client(
        chat_output=_make_openai_chat_response(
            tool_calls=[{"name": "search_servants", "id": "call_abc", "arguments": '{"class":"saber"}'}],
        )
    )
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)

    result = run(
        llm_client.agent_completion(
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
    mock_client = _make_mock_openai_client(chat_output=_make_openai_chat_response(content="这是回复"))
    monkeypatch.setattr(llm_client, "_get_openai_client", lambda p: mock_client)

    result = run(
        llm_client.agent_completion(
            messages=[{"role": "user", "content": "你好"}],
            tools=[{"type": "function", "function": {"name": "search_servants"}}],
            model="model-a1",
        )
    )

    assert result["has_tool_call"] is False
    assert result["output_text"] == "这是回复"
