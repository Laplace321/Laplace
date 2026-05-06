import asyncio

import httpx
import pytest

import server.llm_client as llm_client

VALID_JSON = '{"intent":"query_servants","conditions":{"npCharge":{"op":"eq","value":30}}}'


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

    def __init__(self, timeout, **kwargs):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json, headers):
        self.requests.append(json)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    FakeAsyncClient.requests = []
    FakeAsyncClient.responses = []
    monkeypatch.setattr(llm_client.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(llm_client, "PRIMARY_MODEL", "primary")
    monkeypatch.setattr(llm_client, "FALLBACK_MODELS", ["fallback"])


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

    result = run(llm_client.chat_completion("system", "user", model="primary"))

    assert result["_model"] == "primary"
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

    result = run(llm_client.chat_completion("system", "user", model="primary"))

    assert result["_response_format"] == "text_fallback"
    # 第一次请求使用 text.format
    assert "text" in FakeAsyncClient.requests[0]
    assert "format" in FakeAsyncClient.requests[0]["text"]
    # 降级后不使用 text.format
    assert "text" not in FakeAsyncClient.requests[1] or "format" not in FakeAsyncClient.requests[1].get("text", {})


def test_chat_completion_tries_fallback_model_after_schema_failure():
    FakeAsyncClient.responses = [
        FakeResponse(content='{"intent":"unknown","conditions":{}}'),
        FakeResponse(content=VALID_JSON),
    ]

    result = run(llm_client.chat_completion("system", "user", model="primary"))

    assert result["_model"] == "fallback"
    assert len(FakeAsyncClient.requests) == 2
