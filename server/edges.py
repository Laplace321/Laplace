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
    """Stage 0 分类后路由：A→route / B→atlas / C→guide / FALLBACK→template_fallback / 低置信度→agent_fallback。

    Task 4 Batch B：当 turn_type ∈ {MINOR, CORRECTION} 且存在 prev_turn 时，
    跳过 route_node 直接走 merge_filters 节点合并 delta。

    ADR-032：当 classifier 输出 ``pipeline=FALLBACK`` 时，根据 ``state.extras["fallback_code"]``
    构造 ``routing_result.fallback`` 并直接路由到 ``template_fallback`` 节点输出预置文案。
    """
    pipeline = state.classified_pipeline
    if pipeline == "FALLBACK":
        code = state.extras.get("fallback_code") or "greeting"
        # template_fallback_node 从 routing_result.fallback.code 读取索引模板
        state.extras["routing_result"] = {
            "fallback": {"code": code, "message": ""},
            "skill_calls": [],
        }
        return "template_fallback"
    if pipeline == "B":
        return "atlas"
    if pipeline == "C":
        return "guide"
    if pipeline == "A" and state.classifier_confidence < 0.6:
        state.extras["bail_out"] = "low_confidence_agent"
        return "agent_fallback"
    # 多轮 MINOR / CORRECTION：在 prev_turn 上做 delta 合并，跳过 route 重排
    if pipeline == "A" and state.turn_type in ("MINOR", "CORRECTION") and "prev_turn" in state.extras:
        return "merge_filters"
    return "route"


def after_route(state: PipelineState) -> str:
    """Stage 1 路由后：bail_out → 对应降级节点；否则 → execute。"""
    reason = state.extras.get("bail_out")
    if reason:
        return _dispatch_bail_out(reason)
    return "execute"


def after_merge_filters(state: PipelineState) -> str:
    """MINOR 合并后：成功 → execute；失败 → route（按 MAJOR 重新走标准路由）。

    merge_filters_node 失败时会 ``state.extras["bail_out"]="merge_failed_fallback_route"``，
    此处特判把它转成 route 重路由（其余 bail_out 走标准降级分发）。
    """
    reason = state.extras.get("bail_out")
    if reason == "merge_failed_fallback_route":
        # 已由 merge_filters_node 清掉 prev_turn 并把 turn_type 重置为 MAJOR
        state.extras.pop("bail_out", None)
        return "route"
    if reason:
        return _dispatch_bail_out(reason)
    return "execute"


def after_execute(state: PipelineState) -> str:
    """Skill 执行后：bail_out → 对应降级节点；否则 → generate。"""
    reason = state.extras.get("bail_out")
    if reason:
        return _dispatch_bail_out(reason)
    return "generate"
