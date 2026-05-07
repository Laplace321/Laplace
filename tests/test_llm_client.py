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


def test_parse_intent_response_accepts_plain_json():
    parsed = llm_client.parse_intent_response(VALID_JSON)

    assert parsed["intent"] == "query_servants"
    assert parsed["conditions"]["npCharge"]["value"] == 30


def test_parse_intent_response_extracts_fenced_json():
    parsed = llm_client.parse_intent_response(f"```json\n{VALID_JSON}\n```")

    assert parsed["conditions"]["npCharge"]["op"] == "eq"


def test_parse_intent_response_extracts_json_with_surrounding_text():
    parsed = llm_client.parse_intent_response(f"好的，结果如下：\n{VALID_JSON}\n谢谢")

    assert parsed["conditions"]["npCharge"]["value"] == 30


def test_parse_intent_response_rejects_invalid_json_and_schema():
    with pytest.raises(ValueError):
        llm_client.parse_intent_response("not json")

    with pytest.raises(ValueError):
        llm_client.parse_intent_response('{"intent":"unknown","conditions":{}}')


def test_chat_completion_uses_structured_response_format():
    FakeAsyncClient.responses = [FakeResponse(content=VALID_JSON)]

    result = run(llm_client.chat_completion("system", "user", model="model-a1"))

    assert result["_model"] == "model-a1"
    assert result["_provider"] == "provider_a"
    assert result["_response_format"] == "json_schema"
    # Responses API 使用 text.format 而非 response_format
    assert FakeAsyncClient.requests[0]["text"]["format"]["type"] == "json_schema"
    # 验证使用 instructions 和 input 参数
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

    result = run(llm_client.chat_completion("system", "user", model="model-a1"))

    assert result["_response_format"] == "text_fallback"
    # 第一次请求使用 text.format
    assert "text" in FakeAsyncClient.requests[0]
    assert "format" in FakeAsyncClient.requests[0]["text"]
    # 降级后不使用 text.format
    assert "text" not in FakeAsyncClient.requests[1] or "format" not in FakeAsyncClient.requests[1].get("text", {})


def test_chat_completion_fallback_within_same_provider():
    """同提供商内模型降级：model-a1 失败 → model-a2 成功。"""
    FakeAsyncClient.responses = [
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content=VALID_JSON),
    ]

    result = run(llm_client.chat_completion("system", "user"))

    assert result["_model"] == "model-a2"
    assert result["_provider"] == "provider_a"
    assert len(FakeAsyncClient.requests) == 2


def test_chat_completion_cross_provider_fallback(monkeypatch):
    """跨提供商降级：provider_a 全部失败 → 自动切换 provider_b。"""
    monkeypatch.setattr(llm_client, "PROVIDERS", [PROVIDER_A, PROVIDER_B])
    FakeAsyncClient.responses = [
        # provider_a model-a1 失败
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        # provider_a model-a2 失败
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        # provider_b model-b1 成功
        FakeResponse(content=VALID_JSON),
    ]

    result = run(llm_client.chat_completion("system", "user"))

    assert result["_model"] == "model-b1"
    assert result["_provider"] == "provider_b"
    # 验证请求发送到了正确的 URL
    assert FakeAsyncClient.posted_urls[0] == "https://a.test/v1/responses"
    assert FakeAsyncClient.posted_urls[2] == "https://b.test/v1/responses"


def test_attempts_log_includes_provider_field():
    """_attempts 日志包含 provider 字段。"""
    FakeAsyncClient.responses = [
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content=VALID_JSON),
    ]

    result = run(llm_client.chat_completion("system", "user"))

    assert len(result["_attempts"]) == 1
    assert result["_attempts"][0]["provider"] == "provider_a"
    assert result["_attempts"][0]["model"] == "model-a1"
    assert "error" in result["_attempts"][0]


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
