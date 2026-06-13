"""集中存放 Pipeline 图的条件边路由函数（ADR-028）。

每个 ``after_*`` 函数签名为 ``(state: PipelineState) -> str``，返回下一节点名或 ``END``。
所有路由判断都基于 state 字段（含 state.extras["bail_out"]）做纯函数计算，
不进行 IO / LLM / DB 等副作用，便于单元测试。

Task 3（ADR-028）后，bail_out 不再直接终止图，而是分发到 agent / clarify / fallback
三个降级节点处理；节点处理完毕写 state.reply / servants / query 后再连 END。
"""

from __future__ import annotations

from server.graph.state import PipelineState

# bail_out 分发映射（Task 3）
_AGENT_REASONS = frozenset(
    {
        "low_confidence_agent",
        "routing_failed",
        "fallback_no_match",
        "fallback_ambiguous",
        "empty_skill_calls",
        "execution_fallback",
    }
)
_CLARIFY_REASONS = frozenset({"clarification", "execution_clarification"})
_TEMPLATE_REASONS = frozenset({"fallback_greeting", "fallback_out_of_scope"})


def _dispatch_bail_out(reason: str) -> str:
    """根据 bail_out 原因分发到对应降级节点。

    未知 reason 走 agent 兜底，保证图引擎不会卡在无效边上。
    """
    if reason in _CLARIFY_REASONS:
        return "clarify"
    if reason in _TEMPLATE_REASONS:
        return "template_fallback"
    if reason in _AGENT_REASONS:
        return "agent_fallback"
    # 未知 reason 兜底：交给 agent 节点统一处理
    return "agent_fallback"


def after_classify(state: PipelineState) -> str:
    """Stage 0 分类后路由：A→route / B→atlas / C→guide / 低置信度→agent_fallback。"""
    pipeline = state.classified_pipeline
    if pipeline == "B":
        return "atlas"
    if pipeline == "C":
        return "guide"
    if pipeline == "A" and state.classifier_confidence < 0.6:
        state.extras["bail_out"] = "low_confidence_agent"
        return "agent_fallback"
    return "route"


def after_route(state: PipelineState) -> str:
    """Stage 1 路由后：bail_out → 对应降级节点；否则 → execute。"""
    reason = state.extras.get("bail_out")
    if reason:
        return _dispatch_bail_out(reason)
    return "execute"


def after_execute(state: PipelineState) -> str:
    """Skill 执行后：bail_out → 对应降级节点；否则 → generate。"""
    reason = state.extras.get("bail_out")
    if reason:
        return _dispatch_bail_out(reason)
    return "generate"
