"""Pipeline A Stage 1 节点 — Skill 路由 + 参数提取（ADR-024）。

迁移自 ``server/pipeline.py`` 的 Stage 1 路由逻辑。本节点行为与原代码完全等价：
- 调用 ``build_routing_prompt`` 提示词，最多 2 次 LLM 重试
- 主路径成功：写 state.skill_calls / response_skill_name / target_pipeline / model_used
- 降级条件 → state.extras["bail_out"] = <reason> 让 ``edges.after_route`` 跳到 END：
  - "routing_failed"   2 次重试均失败
  - "clarification"    routing_result.clarification 非空
  - "fallback_<code>"  fallback 字段非空（greeting/out_of_scope/no_match/ambiguous）
  - "empty_skill_calls" 主路径无 fallback 但 skill_calls 为空

bail_out 时把已计算的中间产物存入 state.extras["routing_result"] / extras["routing_error"]。
"""

from __future__ import annotations

from server.graph.decorators import with_trace
from server.graph.state import PipelineState
from server.llm import chat_completion
from server.logger import Phase, log_trace_event
from server.prompts import build_routing_prompt
from server.schemas import (
    parse_routing_response,
    routing_response_json_schema,
)
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.translation import describe_filters


@with_trace(Phase.NODE_ROUTE)
async def route_node(state: PipelineState) -> PipelineState:
    """Stage 1：Skill 路由（仅 A 链路）。

    Note: ``routing_input`` 事件由调用方（``handle_skill_mode``）在 graph run 前统一记录，
    以保持与原代码一致的事件顺序（routing_input → classifier_output → routing_output）。
    """
    streaming = bool(state.extras.get("streaming"))
    if streaming:
        state.pending_events.append(
            {"type": "thinking", "data": {"phase": "routing", "message": "正在理解你的问题..."}}
        )

    skill_descriptions = [
        {"name": s.name, "description": s.description} for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)
    ]

    routing_prompt = build_routing_prompt(skill_descriptions)

    # 路由失败重试：最多 2 次尝试
    routing_result = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            routing_result = await chat_completion(
                system_prompt=routing_prompt,
                user_message=state.user_message,
                temperature=0.1,
                json_mode=True,
                response_schema=routing_response_json_schema,
                response_validator=parse_routing_response,
            )
            break
        except Exception as retry_err:  # noqa: BLE001
            last_error = retry_err
            if attempt == 0:
                print(f"⚠️ [{state.trace_id}] Stage 1 路由第 1 次尝试失败，重试中: {retry_err}")

    # 2 次路由均失败 → bail_out
    if routing_result is None:
        print(f"⚠️ [{state.trace_id}] 路由 2 次均失败，降级到 Agent: {last_error}")
        state.extras["bail_out"] = "routing_failed"
        state.extras["routing_error"] = str(last_error) if last_error else "unknown"
        return state

    model_used = routing_result.pop("_model", "unknown")
    routing_result.pop("_response_format", None)
    routing_result.pop("_provider", None)
    routing_result.pop("_attempts", None)
    routing_usage = routing_result.pop("_usage", {})
    state.trace_total_tokens += routing_usage.get("total_tokens", 0)
    state.model_used = model_used

    skill_calls = routing_result.get("skill_calls", [])
    response_skill_name = routing_result.get("response_skill", "respond_servant_list")

    # ── Trace: routing_output ──
    await log_trace_event(
        state.trace_id,
        "routing_output",
        {
            "skill_calls": skill_calls,
            "response_skill": response_skill_name,
            "fallback": routing_result.get("fallback"),
            "model": model_used,
            "routing_usage": routing_usage,
            "target_pipeline": routing_result.get("target_pipeline"),
        },
    )

    # ── 用户确认机制：检测 clarification ──
    clarification = routing_result.get("clarification")
    if clarification:
        state.extras["bail_out"] = "clarification"
        state.extras["routing_result"] = routing_result
        return state

    # 检查 fallback
    fallback = routing_result.get("fallback")
    if fallback is not None:
        fb_code = fallback.get("code", "no_match")
        state.extras["bail_out"] = f"fallback_{fb_code}"
        state.extras["routing_result"] = routing_result
        return state

    # 空 skill_calls 且无 fallback → bail_out
    if not skill_calls:
        state.extras["bail_out"] = "empty_skill_calls"
        state.extras["routing_result"] = routing_result
        return state

    # 主路径：保存 skill_calls / response_skill_name 供 execute_node 使用
    state.skill_calls = skill_calls
    state.response_skill_name = response_skill_name
    state.target_pipeline = routing_result.get("target_pipeline", "A") or "A"
    state.extras["routing_result"] = routing_result

    # 推送路由完成 thinking（与原 SSE 中文描述一致）
    if streaming:
        state.pending_events.append(
            {
                "type": "thinking",
                "data": {
                    "phase": "routed",
                    "message": "意图识别完成",
                    "detail": "、".join(describe_filters(skill_calls)),
                },
            }
        )
    return state
