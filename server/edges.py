"""集中存放 Pipeline 图的条件边路由函数（ADR-028）。

每个 ``after_*`` 函数签名为 ``(state: PipelineState) -> str``，返回下一节点名或 ``END``。
所有路由判断都基于 state 字段（含 state.extras["bail_out"]）做纯函数计算，
不进行 IO / LLM / DB 等副作用，便于单元测试。
"""

from __future__ import annotations

from server.graph import END
from server.graph.state import PipelineState


def after_classify(state: PipelineState) -> str:
    """Stage 0 分类后路由：A→route / B→atlas / C→guide / 低置信度→END(bail_out)。"""
    pipeline = state.classified_pipeline
    if pipeline == "B":
        return "atlas"
    if pipeline == "C":
        return "guide"
    # A 链路低置信度：bail_out 由 handle_skill_mode 走 Agent fallback（Task 3 节点化）
    if pipeline == "A" and state.classifier_confidence < 0.6:
        state.extras["bail_out"] = "low_confidence_agent"
        return END
    # A 链路高置信度 → 进入 Stage 1 路由
    return "route"


def after_route(state: PipelineState) -> str:
    """Stage 1 路由后：bail_out → END；否则 → execute。"""
    if state.extras.get("bail_out"):
        return END
    return "execute"


def after_execute(state: PipelineState) -> str:
    """Skill 执行后：bail_out → END；否则 → generate。"""
    if state.extras.get("bail_out"):
        return END
    return "generate"
