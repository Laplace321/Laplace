"""
Laplace — LLM 错误分类与重试策略单元测试

测试 provider.py 中 _classify_llm_error() 的错误分类逻辑，
以及 chat_completion() / agent_completion() 的差异化重试行为：
- auth / bad_request → 跳过当前 provider 所有模型
- rate_limit → 指数退避后重试 1 次
- timeout / server_error → 立即尝试下一个模型
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

import server.llm.provider as llm_provider
from server.llm.base import LLMResponseFormatUnsupported
from server.llm.provider import (
    ERROR_AUTH,
    ERROR_BAD_REQUEST,
    ERROR_RATE_LIMIT,
    ERROR_SERVER,
    ERROR_TIMEOUT,
    ERROR_UNKNOWN,
    LLMProvider,
    _classify_llm_error,
    chat_completion,
)

# ── 测试用 httpx 对象工厂 ──

_FAKE_REQUEST = httpx.Request("POST", "https://test.api/v1/chat")


def _make_httpx_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code, request=_FAKE_REQUEST)


# ── _classify_llm_error 单元测试 ──


class TestClassifyLlmError:
    """测试错误分类函数对各类异常的判断。"""

    def test_openai_rate_limit_error(self):
        exc = openai.RateLimitError(
            message="rate limit exceeded",
            response=_make_httpx_response(429),
            body=None,
        )
        assert _classify_llm_error(exc) == ERROR_RATE_LIMIT

    def test_openai_auth_error(self):
        exc = openai.AuthenticationError(
            message="invalid api key",
            response=_make_httpx_response(401),
            body=None,
        )
        assert _classify_llm_error(exc) == ERROR_AUTH

    def test_openai_bad_request_error(self):
        exc = openai.BadRequestError(
            message="bad request",
            response=_make_httpx_response(400),
            body=None,
        )
        assert _classify_llm_error(exc) == ERROR_BAD_REQUEST

    def test_openai_timeout_error(self):
        exc = openai.APITimeoutError(request=_FAKE_REQUEST)
        assert _classify_llm_error(exc) == ERROR_TIMEOUT

    def test_openai_connection_error(self):
        exc = openai.APIConnectionError(request=_FAKE_REQUEST)
        assert _classify_llm_error(exc) == ERROR_TIMEOUT

    def test_openai_internal_server_error(self):
        exc = openai.InternalServerError(
            message="internal error",
            response=_make_httpx_response(500),
            body=None,
        )
        assert _classify_llm_error(exc) == ERROR_SERVER

    def test_llm_response_format_unsupported(self):
        exc = LLMResponseFormatUnsupported("model does not support json_schema")
        assert _classify_llm_error(exc) == ERROR_BAD_REQUEST

    def test_dashscope_throttling(self):
        exc = Exception("dashscope API 错误 [Throttling]: Rate limit exceeded")
        assert _classify_llm_error(exc) == ERROR_RATE_LIMIT

    def test_dashscope_throttling_rate_quota(self):
        exc = Exception("dashscope API 错误 [Throttling.RateQuota]: Too many requests")
        assert _classify_llm_error(exc) == ERROR_RATE_LIMIT

    def test_dashscope_invalid_api_key(self):
        exc = Exception("dashscope API 错误 [InvalidApiKey]: Invalid API-key provided.")
        assert _classify_llm_error(exc) == ERROR_AUTH

    def test_dashscope_arrearage(self):
        exc = Exception("dashscope API 错误 [Arrearage]: Account balance insufficient")
        assert _classify_llm_error(exc) == ERROR_AUTH

    def test_dashscope_bad_request(self):
        exc = Exception("dashscope API 错误 [BadRequest.EmptyInput]: Input is empty")
        assert _classify_llm_error(exc) == ERROR_BAD_REQUEST

    def test_dashscope_invalid_parameter(self):
        exc = Exception("dashscope API 错误 [InvalidParameter]: Invalid parameter")
        assert _classify_llm_error(exc) == ERROR_BAD_REQUEST

    def test_dashscope_internal_error(self):
        exc = Exception("dashscope API 错误 [InternalError]: Server error")
        assert _classify_llm_error(exc) == ERROR_SERVER

    def test_dashscope_timeout(self):
        exc = Exception("dashscope API 错误 [RequestTimeout]: Request timed out")
        assert _classify_llm_error(exc) == ERROR_TIMEOUT

    def test_generic_timeout_message(self):
        exc = Exception("Connection timed out after 30s")
        assert _classify_llm_error(exc) == ERROR_TIMEOUT

    def test_generic_rate_limit_message(self):
        exc = Exception("Rate limit exceeded, please retry later")
        assert _classify_llm_error(exc) == ERROR_RATE_LIMIT

    def test_unknown_error(self):
        exc = Exception("Something unexpected happened")
        assert _classify_llm_error(exc) == ERROR_UNKNOWN


# ── chat_completion 重试行为集成测试 ──


def _make_test_provider(name: str = "test_provider", models: list[str] | None = None) -> LLMProvider:
    """创建带 mock adapter 的测试 provider。"""
    models = models or ["model-a", "model-b"]
    provider = LLMProvider(name=name, base_url="https://test/v1", api_key="test-key", models=models)
    provider.adapter = AsyncMock()
    return provider


@pytest.mark.asyncio
async def test_auth_error_skips_remaining_models():
    """认证错误应跳过当前 provider 的所有剩余模型。"""
    provider = _make_test_provider(models=["model-a", "model-b", "model-c"])
    provider.adapter.chat_completion = AsyncMock(
        side_effect=openai.AuthenticationError(
            message="invalid key",
            response=_make_httpx_response(401),
            body=None,
        )
    )

    with patch.object(llm_provider, "PROVIDERS", [provider]):
        with pytest.raises(Exception, match="所有模型都调用失败"):
            await chat_completion(
                system_prompt="test",
                user_message="test",
                json_mode=False,
            )

    # 认证错误应只尝试第一个模型，跳过其余
    assert provider.adapter.chat_completion.call_count == 1


@pytest.mark.asyncio
async def test_bad_request_skips_remaining_models():
    """参数错误应跳过当前 provider 的所有剩余模型。"""
    provider = _make_test_provider(models=["model-a", "model-b"])
    provider.adapter.chat_completion = AsyncMock(
        side_effect=openai.BadRequestError(
            message="bad request",
            response=_make_httpx_response(400),
            body=None,
        )
    )

    with patch.object(llm_provider, "PROVIDERS", [provider]):
        with pytest.raises(Exception, match="所有模型都调用失败"):
            await chat_completion(
                system_prompt="test",
                user_message="test",
                json_mode=False,
            )

    assert provider.adapter.chat_completion.call_count == 1


@pytest.mark.asyncio
async def test_timeout_tries_next_model():
    """超时错误应立即尝试下一个模型。"""
    provider = _make_test_provider(models=["model-a", "model-b"])
    success_result = {"text": "ok", "_model": "model-b"}

    provider.adapter.chat_completion = AsyncMock(
        side_effect=[
            openai.APITimeoutError(request=_FAKE_REQUEST),
            success_result,
        ]
    )

    with patch.object(llm_provider, "PROVIDERS", [provider]):
        result = await chat_completion(
            system_prompt="test",
            user_message="test",
            json_mode=False,
        )

    assert result["text"] == "ok"
    assert provider.adapter.chat_completion.call_count == 2


@pytest.mark.asyncio
async def test_rate_limit_retries_with_backoff():
    """限流错误应退避后重试同一模型 1 次。"""
    provider = _make_test_provider(models=["model-a"])
    success_result = {"text": "ok after retry", "_model": "model-a"}

    # 第一次限流，第二次成功
    provider.adapter.chat_completion = AsyncMock(
        side_effect=[
            openai.RateLimitError(
                message="rate limit",
                response=_make_httpx_response(429),
                body=None,
            ),
            success_result,
        ]
    )

    with (
        patch.object(llm_provider, "PROVIDERS", [provider]),
        patch("server.llm.provider.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        result = await chat_completion(
            system_prompt="test",
            user_message="test",
            json_mode=False,
        )

    assert result["text"] == "ok after retry"
    assert provider.adapter.chat_completion.call_count == 2
    # 验证退避等待被调用
    mock_sleep.assert_called_once_with(2.0)


@pytest.mark.asyncio
async def test_rate_limit_retry_fails_then_next_model():
    """限流重试仍失败，应继续尝试下一个模型。"""
    provider = _make_test_provider(models=["model-a", "model-b"])
    rate_limit_exc = openai.RateLimitError(
        message="rate limit",
        response=_make_httpx_response(429),
        body=None,
    )
    success_result = {"text": "fallback ok", "_model": "model-b"}

    # model-a: 限流 → 重试仍限流; model-b: 成功
    provider.adapter.chat_completion = AsyncMock(side_effect=[rate_limit_exc, rate_limit_exc, success_result])

    with (
        patch.object(llm_provider, "PROVIDERS", [provider]),
        patch("server.llm.provider.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await chat_completion(
            system_prompt="test",
            user_message="test",
            json_mode=False,
        )

    assert result["text"] == "fallback ok"
    # model-a 尝试 2 次（原始 + 限流重试），model-b 尝试 1 次
    assert provider.adapter.chat_completion.call_count == 3


@pytest.mark.asyncio
async def test_server_error_falls_through_to_next_provider():
    """服务端错误应穿透到下一个 provider。"""
    provider_a = _make_test_provider(name="provider_a", models=["model-a"])
    provider_b = _make_test_provider(name="provider_b", models=["model-b"])

    provider_a.adapter.chat_completion = AsyncMock(
        side_effect=openai.InternalServerError(
            message="internal error",
            response=_make_httpx_response(500),
            body=None,
        )
    )
    provider_b.adapter.chat_completion = AsyncMock(return_value={"text": "provider b ok", "_model": "model-b"})

    with patch.object(llm_provider, "PROVIDERS", [provider_a, provider_b]):
        result = await chat_completion(
            system_prompt="test",
            user_message="test",
            json_mode=False,
        )

    assert result["text"] == "provider b ok"
    assert provider_a.adapter.chat_completion.call_count == 1
    assert provider_b.adapter.chat_completion.call_count == 1


@pytest.mark.asyncio
async def test_attempts_log_includes_error_type():
    """attempts_log 应包含 error_type 字段。"""
    provider = _make_test_provider(models=["model-a"])
    provider.adapter.chat_completion = AsyncMock(side_effect=openai.APITimeoutError(request=_FAKE_REQUEST))

    with patch.object(llm_provider, "PROVIDERS", [provider]):
        with pytest.raises(Exception) as exc_info:
            await chat_completion(
                system_prompt="test",
                user_message="test",
                json_mode=False,
            )

    error_msg = str(exc_info.value)
    assert "error_type" in error_msg
    assert "timeout" in error_msg


@pytest.mark.asyncio
async def test_dashscope_auth_error_skips_provider():
    """dashscope 认证错误（InvalidApiKey）应跳过整个 provider。"""
    provider = _make_test_provider(name="dashscope_test", models=["qwen-plus", "qwen-turbo"])
    provider.adapter.chat_completion = AsyncMock(
        side_effect=Exception("dashscope API 错误 [InvalidApiKey]: Invalid API-key provided.")
    )

    with patch.object(llm_provider, "PROVIDERS", [provider]):
        with pytest.raises(Exception, match="所有模型都调用失败"):
            await chat_completion(
                system_prompt="test",
                user_message="test",
                json_mode=False,
            )

    # 认证错误应只尝试第一个模型
    assert provider.adapter.chat_completion.call_count == 1
