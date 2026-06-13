"""edges.py 条件边路由测试（Task 2 — ADR-028）。

纯函数测试：在不调用任何 IO/LLM 的前提下，覆盖 ``after_classify`` /
``after_route`` / ``after_execute`` 在各种 ``state`` 组合下的目标节点。
"""

from __future__ import annotations

from server.edges import after_classify, after_execute, after_route
from server.graph import END
from server.graph.state import PipelineState

# ────────────────────────────────────────────────────────────
# after_classify
# ────────────────────────────────────────────────────────────


def test_after_classify_b_routes_to_atlas():
    state = PipelineState(classified_pipeline="B", classifier_confidence=0.95)
    assert after_classify(state) == "atlas"
    # B 走 atlas 不应触发 bail_out
    assert "bail_out" not in state.extras


def test_after_classify_c_routes_to_guide():
    state = PipelineState(classified_pipeline="C", classifier_confidence=0.9)
    assert after_classify(state) == "guide"
    assert "bail_out" not in state.extras


def test_after_classify_a_high_confidence_routes_to_route():
    state = PipelineState(classified_pipeline="A", classifier_confidence=0.85)
    assert after_classify(state) == "route"
    assert "bail_out" not in state.extras


def test_after_classify_a_low_confidence_bails_out():
    state = PipelineState(classified_pipeline="A", classifier_confidence=0.3)
    assert after_classify(state) == END
    assert state.extras["bail_out"] == "low_confidence_agent"


def test_after_classify_a_at_threshold_routes_to_route():
    """confidence == 0.6 应判定为高置信度（>=）。"""
    state = PipelineState(classified_pipeline="A", classifier_confidence=0.6)
    assert after_classify(state) == "route"


# ────────────────────────────────────────────────────────────
# after_route
# ────────────────────────────────────────────────────────────


def test_after_route_no_bail_out_proceeds_to_execute():
    state = PipelineState()
    assert after_route(state) == "execute"


def test_after_route_with_bail_out_routes_to_end():
    for reason in (
        "routing_failed",
        "clarification",
        "fallback_no_match",
        "fallback_greeting",
        "empty_skill_calls",
    ):
        state = PipelineState()
        state.extras["bail_out"] = reason
        assert after_route(state) == END, f"reason={reason!r} 应当返回 END"


# ────────────────────────────────────────────────────────────
# after_execute
# ────────────────────────────────────────────────────────────


def test_after_execute_no_bail_out_proceeds_to_generate():
    state = PipelineState()
    assert after_execute(state) == "generate"


def test_after_execute_with_bail_out_routes_to_end():
    for reason in ("execution_clarification", "execution_fallback"):
        state = PipelineState()
        state.extras["bail_out"] = reason
        assert after_execute(state) == END, f"reason={reason!r} 应当返回 END"
