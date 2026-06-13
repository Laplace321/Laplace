"""edges.py 条件边路由测试（Task 3 — ADR-028）。

纯函数测试：在不调用任何 IO/LLM 的前提下，覆盖 ``after_classify`` /
``after_route`` / ``after_execute`` 在各种 ``state`` 组合下的目标节点。

Task 3 后，bail_out 不再终止图，而是分发到 agent_fallback / clarify / template_fallback
三个降级节点。
"""

from __future__ import annotations

from server.edges import after_classify, after_execute, after_route
from server.graph.state import PipelineState

# ────────────────────────────────────────────────────────────
# after_classify
# ────────────────────────────────────────────────────────────


def test_after_classify_b_routes_to_atlas():
    state = PipelineState(classified_pipeline="B", classifier_confidence=0.95)
    assert after_classify(state) == "atlas"
    assert "bail_out" not in state.extras


def test_after_classify_c_routes_to_guide():
    state = PipelineState(classified_pipeline="C", classifier_confidence=0.9)
    assert after_classify(state) == "guide"
    assert "bail_out" not in state.extras


def test_after_classify_a_high_confidence_routes_to_route():
    state = PipelineState(classified_pipeline="A", classifier_confidence=0.85)
    assert after_classify(state) == "route"
    assert "bail_out" not in state.extras


def test_after_classify_a_low_confidence_routes_to_agent_fallback():
    """A 链路低置信度：直接进入 agent_fallback 节点（Task 3）。"""
    state = PipelineState(classified_pipeline="A", classifier_confidence=0.3)
    assert after_classify(state) == "agent_fallback"
    assert state.extras["bail_out"] == "low_confidence_agent"


def test_after_classify_a_at_threshold_routes_to_route():
    """confidence == 0.6 应判定为高置信度（>=）。"""
    state = PipelineState(classified_pipeline="A", classifier_confidence=0.6)
    assert after_classify(state) == "route"


# ────────────────────────────────────────────────────────────
# after_route — bail_out 分发到 agent / clarify / template_fallback
# ────────────────────────────────────────────────────────────


def test_after_route_no_bail_out_proceeds_to_execute():
    state = PipelineState()
    assert after_route(state) == "execute"


def test_after_route_routing_failed_to_agent_fallback():
    state = PipelineState()
    state.extras["bail_out"] = "routing_failed"
    assert after_route(state) == "agent_fallback"


def test_after_route_clarification_to_clarify():
    state = PipelineState()
    state.extras["bail_out"] = "clarification"
    assert after_route(state) == "clarify"


def test_after_route_fallback_no_match_to_agent_fallback():
    state = PipelineState()
    state.extras["bail_out"] = "fallback_no_match"
    assert after_route(state) == "agent_fallback"


def test_after_route_fallback_ambiguous_to_agent_fallback():
    state = PipelineState()
    state.extras["bail_out"] = "fallback_ambiguous"
    assert after_route(state) == "agent_fallback"


def test_after_route_fallback_greeting_to_template_fallback():
    state = PipelineState()
    state.extras["bail_out"] = "fallback_greeting"
    assert after_route(state) == "template_fallback"


def test_after_route_fallback_out_of_scope_to_template_fallback():
    state = PipelineState()
    state.extras["bail_out"] = "fallback_out_of_scope"
    assert after_route(state) == "template_fallback"


def test_after_route_empty_skill_calls_to_agent_fallback():
    state = PipelineState()
    state.extras["bail_out"] = "empty_skill_calls"
    assert after_route(state) == "agent_fallback"


def test_after_route_unknown_reason_falls_back_to_agent():
    """未知 reason 必须有兜底归宿，避免图卡死。"""
    state = PipelineState()
    state.extras["bail_out"] = "some_unknown_reason"
    assert after_route(state) == "agent_fallback"


# ────────────────────────────────────────────────────────────
# after_execute — execution_* 系列分发
# ────────────────────────────────────────────────────────────


def test_after_execute_no_bail_out_proceeds_to_generate():
    state = PipelineState()
    assert after_execute(state) == "generate"


def test_after_execute_clarification_to_clarify():
    state = PipelineState()
    state.extras["bail_out"] = "execution_clarification"
    assert after_execute(state) == "clarify"


def test_after_execute_fallback_to_agent():
    state = PipelineState()
    state.extras["bail_out"] = "execution_fallback"
    assert after_execute(state) == "agent_fallback"
