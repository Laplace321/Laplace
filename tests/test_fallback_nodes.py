"""降级节点单元测试（Task 3 — ADR-028）。

覆盖 ``server.nodes.agent.agent_fallback_node`` / ``server.nodes.clarify.clarify_node`` /
``server.nodes.fallback.template_fallback_node`` 的关键行为，以确保与原
``_bail_out_to_chat_response`` 等价。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.graph.state import PipelineState
from server.nodes.agent import agent_fallback_node
from server.nodes.clarify import clarify_node
from server.nodes.fallback import template_fallback_node


@dataclass
class _FakeAgentResult:
    """构造 agent_loop.AgentResult 的最小替身。"""

    reply: str = "不能识别这个查询。"
    rounds: int = 2
    total_tokens: int = 123
    tool_trace: list[dict] = None
    servants_data: list[dict] = None
    elapsed_ms: float = 12.5

    def __post_init__(self):
        if self.tool_trace is None:
            self.tool_trace = []
        if self.servants_data is None:
            self.servants_data = []


@dataclass
class _FakeExecutorResult:
    """构造 SkillExecutor ExecutionResult 的最小替身。"""

    is_fallback: bool = False
    fallback_message: str | None = None
    clarification: dict[str, Any] = None

    def __post_init__(self):
        if self.clarification is None:
            self.clarification = {}


def _base_state(reason: str, **extras_kwargs) -> PipelineState:
    state = PipelineState(
        user_message="test",
        trace_id="t-fallback",
        request_start=time.monotonic(),
        client_ip="127.0.0.1",
        model_used="skill_mode",
    )
    state.extras["bail_out"] = reason
    state.extras.update(extras_kwargs)
    return state


# ────────────────────────────────────────────────────────────
# agent_fallback_node — 6 个 reason
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_node_low_confidence_success():
    state = _base_state("low_confidence_agent")
    state.classifier_confidence = 0.4
    fake = _FakeAgentResult(reply="问候回复", rounds=1, total_tokens=50)
    with patch("server.nodes.agent.agent_route", new=AsyncMock(return_value=fake)):
        out = await agent_fallback_node(state)
    assert out.reply == "问候回复"
    assert out.count == 0
    assert out.query["mode"] == "agent_fallback"
    assert out.query["classifier_confidence"] == 0.4
    assert out.model_used == "agent_1r"


@pytest.mark.asyncio
async def test_agent_node_routing_failed_error_uses_error_model():
    state = _base_state("routing_failed", routing_error="boom")
    with patch("server.nodes.agent.agent_route", new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await agent_fallback_node(state)
    assert out.reply == "抱歉，Skill 路由遇到问题，请稍后重试。"
    assert out.model_used == "error"
    assert out.query == {}


@pytest.mark.asyncio
async def test_agent_node_fallback_no_match_uses_routing_query_on_error():
    routing_result = {
        "fallback": {"code": "no_match", "message": "未找到结果，请尝试更具体的描述。"},
    }
    state = _base_state("fallback_no_match", routing_result=routing_result)
    with patch("server.nodes.agent.agent_route", new=AsyncMock(side_effect=RuntimeError("err"))):
        out = await agent_fallback_node(state)
    assert out.reply == "未找到结果，请尝试更具体的描述。"
    assert out.query == routing_result
    # 默认 error_model=None：保留 state.model_used="skill_mode"
    assert out.model_used == "skill_mode"


@pytest.mark.asyncio
async def test_agent_node_empty_skill_calls_default_message():
    state = _base_state("empty_skill_calls", routing_result={"foo": "bar"})
    with patch("server.nodes.agent.agent_route", new=AsyncMock(side_effect=RuntimeError("err"))):
        out = await agent_fallback_node(state)
    assert out.reply == "无法从你的问题中识别出查询条件，请尝试更具体的描述。"
    assert out.query == {"foo": "bar"}


@pytest.mark.asyncio
async def test_agent_node_execution_fallback_passes_oneshot_context():
    """execution_fallback 必须把 build_oneshot_context 结果作为 oneshot_context 传给 agent_route。"""
    fake = _FakeAgentResult(reply="智能搜索结果", rounds=3, servants_data=[{"id": 1}, {"id": 2}])
    state = _base_state("execution_fallback")
    state.skill_calls = [{"skill_name": "search_servants", "params": {}}]
    state.extras["executor_result"] = _FakeExecutorResult(is_fallback=True, fallback_message="未找到结果。")

    captured: dict[str, Any] = {}

    async def fake_agent_route(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fake

    with patch("server.nodes.agent.agent_route", side_effect=fake_agent_route):
        out = await agent_fallback_node(state)

    assert "oneshot_context" in captured["kwargs"]
    assert out.reply == "智能搜索结果"
    assert out.count == 2
    assert out.model_used == "agent_3r"
    assert out.query == {"mode": "agent_fallback"}


@pytest.mark.asyncio
async def test_agent_node_execution_fallback_error_keeps_executor_message():
    state = _base_state("execution_fallback")
    state.skill_calls = []
    state.extras["executor_result"] = _FakeExecutorResult(is_fallback=True, fallback_message="未找到XXX。")
    with patch("server.nodes.agent.agent_route", new=AsyncMock(side_effect=RuntimeError("err"))):
        out = await agent_fallback_node(state)
    assert out.reply == "未找到XXX。"
    assert out.query == {"mode": "execution_fallback"}


# ────────────────────────────────────────────────────────────
# clarify_node — routing + execution clarification
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_node_routing_clarification():
    routing_result = {
        "clarification": {
            "question": "请选择具体从者",
            "options": [{"id": 100, "label": "BB"}],
            "ambiguous_field": "name",
        }
    }
    state = _base_state("clarification", routing_result=routing_result)
    out = await clarify_node(state)
    assert out.reply == ""
    assert out.count == 0
    assert out.query["mode"] == "clarification"
    assert out.query["clarification"] == routing_result["clarification"]
    assert "source" not in out.query  # routing 路径不带 source


@pytest.mark.asyncio
async def test_clarify_node_execution_clarification_marks_source():
    clarification = {
        "type": "multi_match",
        "question": "找到多个候选，请选择",
        "options": [{"id": 1}],
        "ambiguous_field": "name",
    }
    state = _base_state("execution_clarification")
    state.extras["executor_result"] = _FakeExecutorResult(clarification=clarification)
    out = await clarify_node(state)
    assert out.query["mode"] == "clarification"
    assert out.query["source"] == "execution"
    assert out.query["clarification"]["question"] == "找到多个候选，请选择"


# ────────────────────────────────────────────────────────────
# template_fallback_node — greeting / out_of_scope
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_template_fallback_greeting_uses_template():
    routing_result = {"fallback": {"code": "greeting", "message": "fallback msg"}}
    state = _base_state("fallback_greeting", routing_result=routing_result)
    out = await template_fallback_node(state)
    # GREETING 模板包含「Laplace」字样
    assert "Laplace" in out.reply
    assert out.query == routing_result
    assert out.count == 0


@pytest.mark.asyncio
async def test_template_fallback_out_of_scope_uses_template():
    routing_result = {"fallback": {"code": "out_of_scope", "message": "fallback msg"}}
    state = _base_state("fallback_out_of_scope", routing_result=routing_result)
    out = await template_fallback_node(state)
    assert "FGO" in out.reply  # OUT_OF_SCOPE 模板特征
    assert out.query == routing_result


@pytest.mark.asyncio
async def test_template_fallback_unknown_code_falls_back_to_message():
    """fb_code 不在 FALLBACK_TEMPLATES 时使用 fallback.message。"""
    routing_result = {"fallback": {"code": "no_match", "message": "原始错误回复"}}
    state = _base_state("fallback_greeting", routing_result=routing_result)
    out = await template_fallback_node(state)
    assert out.reply == "原始错误回复"
