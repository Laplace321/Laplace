"""
Agent Loop 单元测试。

测试范围：
- AgentResult 数据结构
- agent_route 多轮循环逻辑（mock LLM）
- max_rounds 保护
- tool call 解析与 handler 执行
- 异常处理（LLM 调用失败、handler 执行失败）

所有测试 mock LLM 调用，纯确定性测试。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from server.agent.agent_loop import AgentResult, agent_route

# ============================================================
# AgentResult 数据结构测试
# ============================================================


class TestAgentResult:
    """测试 AgentResult 数据结构。"""

    def test_default_values(self):
        result = AgentResult(reply="test")
        assert result.reply == "test"
        assert result.tool_trace == []
        assert result.rounds == 0
        assert result.total_tokens == 0
        assert result.elapsed_ms == 0.0
        assert result.is_fallback is False
        assert result.servants_data == []

    def test_with_values(self):
        result = AgentResult(
            reply="hello",
            tool_trace=[{"round": 1, "tool": "test"}],
            rounds=2,
            total_tokens=100,
            elapsed_ms=500.0,
            is_fallback=True,
        )
        assert result.rounds == 2
        assert result.total_tokens == 100
        assert result.is_fallback is True
        assert len(result.tool_trace) == 1


# ============================================================
# Agent Loop 主循环测试
# ============================================================


class TestAgentRoute:
    """测试 agent_route 多轮循环逻辑。"""

    @pytest.mark.asyncio
    async def test_direct_message_response(self):
        """LLM 直接返回 message（不调用任何工具）→ 1 轮完成。"""
        mock_response = {
            "output_text": "你好，我是 Laplace！",
            "has_tool_call": False,
            "tool_calls": [],
            "usage": {"total_tokens": 50},
        }

        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response
            result = await agent_route(
                user_message="你好",
                tool_handlers={},
                trace_id="test001",
            )

        assert result.reply == "你好，我是 Laplace！"
        assert result.rounds == 1
        assert result.tool_trace == []
        assert result.is_fallback is False
        assert result.total_tokens == 50

    @pytest.mark.asyncio
    async def test_single_tool_call_then_message(self):
        """LLM 调用 1 个工具 → 收到结果后生成 message → 2 轮完成。"""
        # Round 1: LLM 返回 tool_calls（Chat Completions 格式）
        round1_response = {
            "output_text": None,
            "has_tool_call": True,
            "tool_calls": [
                {
                    "name": "list_classes",
                    "call_id": "call_001",
                    "arguments": "{}",
                }
            ],
            "raw_message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {"name": "list_classes", "arguments": "{}"},
                    }
                ],
            },
            "usage": {"total_tokens": 30},
        }

        # Round 2: LLM 生成最终回复
        round2_response = {
            "output_text": "共有 15 个职阶可供查询。",
            "has_tool_call": False,
            "tool_calls": [],
            "usage": {"total_tokens": 40},
        }

        def mock_handler(params):
            return {"total": 15, "classes": [{"key": "Saber"}]}

        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [round1_response, round2_response]
            result = await agent_route(
                user_message="有哪些职阶？",
                tool_handlers={"list_classes": mock_handler},
                trace_id="test002",
            )

        assert result.reply == "共有 15 个职阶可供查询。"
        assert result.rounds == 2
        assert result.total_tokens == 70
        assert len(result.tool_trace) == 1
        assert result.tool_trace[0]["tool"] == "list_classes"

    @pytest.mark.asyncio
    async def test_max_rounds_protection(self):
        """超过 max_rounds 时强制终止，返回降级回复。"""
        # 每轮都返回 tool_calls，永远不返回 message
        endless_response = {
            "output_text": None,
            "has_tool_call": True,
            "tool_calls": [
                {
                    "name": "list_effects",
                    "call_id": "call_loop",
                    "arguments": "{}",
                }
            ],
            "raw_message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_loop",
                        "type": "function",
                        "function": {"name": "list_effects", "arguments": "{}"},
                    }
                ],
            },
            "usage": {"total_tokens": 20},
        }

        def mock_handler(params):
            return {"total": 0, "effects": []}

        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = endless_response
            result = await agent_route(
                user_message="test",
                tool_handlers={"list_effects": mock_handler},
                trace_id="test003",
                max_rounds=3,
            )

        assert result.is_fallback is True
        assert result.rounds == 3
        assert "复杂" in result.reply or "简化" in result.reply

    @pytest.mark.asyncio
    async def test_llm_call_failure(self):
        """LLM 调用失败时返回降级回复。"""
        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = Exception("API timeout")
            result = await agent_route(
                user_message="test",
                tool_handlers={},
                trace_id="test004",
            )

        assert result.is_fallback is True
        assert result.rounds == 1
        assert "问题" in result.reply or "稍后" in result.reply

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        """LLM 调用不存在的工具时，返回错误信息并继续。"""
        round1_response = {
            "output_text": None,
            "has_tool_call": True,
            "tool_calls": [
                {
                    "name": "nonexistent_tool",
                    "call_id": "call_bad",
                    "arguments": "{}",
                }
            ],
            "raw_message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "nonexistent_tool", "arguments": "{}"},
                    }
                ],
            },
            "usage": {"total_tokens": 20},
        }

        round2_response = {
            "output_text": "抱歉，出现了问题。",
            "has_tool_call": False,
            "tool_calls": [],
            "usage": {"total_tokens": 30},
        }

        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [round1_response, round2_response]
            result = await agent_route(
                user_message="test",
                tool_handlers={},
                trace_id="test005",
            )

        assert result.rounds == 2
        assert len(result.tool_trace) == 1
        assert "error" in result.tool_trace[0]["result_summary"]

    @pytest.mark.asyncio
    async def test_handler_exception_returns_error(self):
        """handler 执行抛异常时，返回错误信息给 LLM 并继续。"""
        round1_response = {
            "output_text": None,
            "has_tool_call": True,
            "tool_calls": [
                {
                    "name": "broken_tool",
                    "call_id": "call_err",
                    "arguments": "{}",
                }
            ],
            "raw_message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_err",
                        "type": "function",
                        "function": {"name": "broken_tool", "arguments": "{}"},
                    }
                ],
            },
            "usage": {"total_tokens": 20},
        }

        round2_response = {
            "output_text": "工具出错了。",
            "has_tool_call": False,
            "tool_calls": [],
            "usage": {"total_tokens": 30},
        }

        def broken_handler(params):
            raise ValueError("数据库连接失败")

        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [round1_response, round2_response]
            result = await agent_route(
                user_message="test",
                tool_handlers={"broken_tool": broken_handler},
                trace_id="test006",
            )

        assert result.rounds == 2
        assert "error" in result.tool_trace[0]["result_summary"]

    @pytest.mark.asyncio
    async def test_async_handler_supported(self):
        """支持异步 handler（如 lookup_skill_detail）。"""
        round1_response = {
            "output_text": None,
            "has_tool_call": True,
            "tool_calls": [
                {
                    "name": "async_tool",
                    "call_id": "call_async",
                    "arguments": '{"id": 1}',
                }
            ],
            "raw_message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_async",
                        "type": "function",
                        "function": {"name": "async_tool", "arguments": '{"id": 1}'},
                    }
                ],
            },
            "usage": {"total_tokens": 20},
        }

        round2_response = {
            "output_text": "异步结果",
            "has_tool_call": False,
            "tool_calls": [],
            "usage": {"total_tokens": 30},
        }

        async def async_handler(params):
            return {"result": "async_ok", "total": 1}

        with patch("server.agent.agent_loop.agent_completion", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [round1_response, round2_response]
            result = await agent_route(
                user_message="test",
                tool_handlers={"async_tool": async_handler},
                trace_id="test007",
            )

        assert result.rounds == 2
        assert result.tool_trace[0]["result_summary"] == "total=1"
