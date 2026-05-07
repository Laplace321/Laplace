import asyncio

import httpx
import pytest

import server.llm_client as llm_client
from server.llm_client import LLMProvider

VALID_JSON = '{"intent":"query_servants","conditions":{"npCharge":{"op":"eq","value":30}}}'

# --- 测试用提供商 ---
PROVIDER_A = LLMProvider(
    name="provider_a", base_url="https://a.test/v1", api_key="key-a", models=["model-a1", "model-a2"]
)
PROVIDER_B = LLMProvider(name="provider_b", base_url="https://b.test/v1", api_key="key-b", models=["model-b1"])


class FakeResponse:
    def __init__(self, status_code=200, content=VALID_JSON, text=None):
        self.status_code = status_code
        self._content = content
        self.text = text if text is not None else str(content)

    def json(self):
        # Responses API 格式：output_text 辅助字段
        return {
            "id": "resp_test_123",
            "object": "response",
            "output_text": self._content,
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": self._content}]}
            ],
        }

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://example.test/responses")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class FakeAsyncClient:
    requests = []
    responses = []
    posted_urls = []

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json, headers):
        self.requests.append(json)
        self.posted_urls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = []
    FakeAsyncClient.posted_urls = []
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(llm_client, "PROVIDERS", [PROVIDER_A])


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


def test_chat_completion_uses_structured_response_format():
    FakeAsyncClient.responses = [FakeResponse(content=VALID_JSON)]

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
    assert FakeAsyncClient.requests[0]["text"]["format"]["type"] == "json_schema"
    assert FakeAsyncClient.requests[0]["instructions"] == "system"
    assert FakeAsyncClient.requests[0]["input"] == "user"


def test_chat_completion_downgrades_when_response_format_is_unsupported():
    FakeAsyncClient.responses = [
        FakeResponse(
            status_code=400,
            content="",
            text='{"error":"text.format json_schema unsupported"}',
        ),
        FakeResponse(content=f"```json\n{VALID_JSON}\n```"),
    ]

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
    assert "text" in FakeAsyncClient.requests[0]
    assert "format" in FakeAsyncClient.requests[0]["text"]
    assert "text" not in FakeAsyncClient.requests[1] or "format" not in FakeAsyncClient.requests[1].get("text", {})


def test_chat_completion_fallback_within_same_provider():
    """同提供商内模型降级：model-a1 失败 → model-a2 成功。"""
    FakeAsyncClient.responses = [
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content=VALID_JSON),
    ]

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
    assert len(FakeAsyncClient.requests) == 2


def test_chat_completion_cross_provider_fallback(monkeypatch):
    """跨提供商降级：provider_a 全部失败 → 自动切换 provider_b。"""
    monkeypatch.setattr(llm_client, "PROVIDERS", [PROVIDER_A, PROVIDER_B])
    FakeAsyncClient.responses = [
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content=VALID_JSON),
    ]

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
    assert FakeAsyncClient.posted_urls[0] == "https://a.test/v1/responses"
    assert FakeAsyncClient.posted_urls[2] == "https://b.test/v1/responses"


def test_attempts_log_includes_provider_field():
    """_attempts 日志包含 provider 字段。"""
    FakeAsyncClient.responses = [
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content=VALID_JSON),
    ]

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
    FakeAsyncClient.responses = [FakeResponse(content=VALID_JSON)]
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
    assert providers[1].name == "beta"
    assert providers[1].models == ["m3"]


# --- 迭代 2：自定义 response_schema / response_validator 测试 ---

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


def test_custom_schema_is_sent_to_api():
    """自定义 response_schema 应传递到 API 请求的 text.format.schema 中。"""
    FakeAsyncClient.responses = [FakeResponse(content=CUSTOM_JSON)]

    result = run(
        llm_client.chat_completion(
            "system",
            "user",
            model="model-a1",
            response_schema=_custom_schema,
            response_validator=_custom_validator,
        )
    )

    # 验证自定义 schema 被传递到 API 请求
    req = FakeAsyncClient.requests[0]
    schema = req["text"]["format"]["schema"]
    assert schema["properties"]["action"]["type"] == "string"
    assert schema["properties"]["query"]["type"] == "string"

    # 验证自定义 validator 正确解析响应
    assert result["action"] == "search"
    assert result["query"] == "saber"
    assert result["_model"] == "model-a1"


def test_custom_validator_is_used_for_parsing():
    """自定义 response_validator 替代旧默认的 parse_intent_response。"""
    FakeAsyncClient.responses = [FakeResponse(content=CUSTOM_JSON)]

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
