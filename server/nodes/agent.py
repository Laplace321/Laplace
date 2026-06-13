"""Pipeline A 降级节点 — Agent 兜底（4 处合 1 — ADR-028 Task 3）。

迁移自 ``server/pipeline.py`` 的 ``_bail_out_to_chat_response`` + ``_agent_fallback_response``，
统一收敛 6 种需要走 Agent 兜底的降级原因到 ``agent_fallback_node``：

- ``low_confidence_agent``   Stage 0 分类 A 但置信度过低
- ``routing_failed``         Stage 1 路由 2 次重试均失败
- ``fallback_no_match``      路由返回 fallback.code=no_match
- ``fallback_ambiguous``     路由返回 fallback.code=ambiguous
- ``empty_skill_calls``      路由返回空 skill_calls 且无 fallback
- ``execution_fallback``     SkillExecutor 返回 is_fallback（含昵称识别后仍失败）

节点行为：调用 ``agent_route`` 走 Agent ReAct → 写 state.reply / servants / count /
query / model_used，作为终态节点直接连 END。Agent 失败时按各 reason 的默认配置返回模板。
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator

from server.agent.agent_loop import AgentResult, agent_route
from server.agent.tool_handlers import TOOL_HANDLERS
from server.context_builder import MAX_RESULTS
from server.fallback import build_oneshot_context, classify_agent_reply
from server.graph.decorators import with_trace
from server.graph.state import PipelineState
from server.logger import Phase, log_trace_event


def _build_agent_config(state: PipelineState) -> dict:
    """根据 state.extras["bail_out"] 计算 Agent 调用所需的 trace 标签 / 错误兜底等参数。

    返回字段：
    - ``result_label``         Agent 成功后 trace.final.result 的值
    - ``extra_query_mode``     Agent 成功后 ChatResponse.query.mode
    - ``extra_query_extra``    成功时 query 字段附加内容
    - ``error_fallback_reply`` Agent 失败时的兜底回复
    - ``error_query``          Agent 失败时的 ChatResponse.query
    - ``error_result``         失败时 trace.final.result
    - ``error_mode``           失败时 trace.final.mode
    - ``error_model``          失败时 ChatResponse.model（None=保留 state.model_used）
    - ``oneshot_context``      Agent 调用时附带的 oneshot 上下文
    """
    reason = state.extras.get("bail_out", "unknown")

    if reason == "low_confidence_agent":
        return {
            "result_label": "classifier_low_confidence_agent",
            "extra_query_mode": "agent_fallback",
            "extra_query_extra": {"classifier_confidence": state.classifier_confidence},
            "error_fallback_reply": "无法处理你的请求，请稍后重试。",
            "error_query": {},
            "error_result": "fallback",
            "error_mode": "fallback_no_match",
            "error_model": None,
            "oneshot_context": None,
        }

    if reason == "routing_failed":
        return {
            "result_label": "routing_retry_agent_fallback",
            "extra_query_mode": "routing_retry_agent_fallback",
            "extra_query_extra": {"routing_error": state.extras.get("routing_error")},
            "error_fallback_reply": "抱歉，Skill 路由遇到问题，请稍后重试。",
            "error_query": {},
            "error_result": "routing_error",
            "error_mode": "routing_error",
            "error_model": "error",
            "oneshot_context": None,
        }

    if reason in ("fallback_no_match", "fallback_ambiguous"):
        routing_result = state.extras.get("routing_result", {}) or {}
        fallback = routing_result.get("fallback", {}) or {}
        fb_msg = fallback.get("message", "无法理解你的问题，请尝试更具体的描述。")
        return {
            "result_label": "agent_fallback",
            "extra_query_mode": "agent_fallback",
            "extra_query_extra": None,
            "error_fallback_reply": fb_msg,
            "error_query": routing_result,
            "error_result": "fallback",
            "error_mode": "fallback_no_match",
            "error_model": None,
            "oneshot_context": None,
        }

    if reason == "empty_skill_calls":
        routing_result = state.extras.get("routing_result", {}) or {}
        return {
            "result_label": "agent_fallback",
            "extra_query_mode": "agent_fallback",
            "extra_query_extra": None,
            "error_fallback_reply": "无法从你的问题中识别出查询条件，请尝试更具体的描述。",
            "error_query": routing_result,
            "error_result": "no_match",
            "error_mode": "fallback_no_match",
            "error_model": None,
            "oneshot_context": None,
        }

    if reason == "execution_fallback":
        result = state.extras.get("executor_result")
        oneshot_ctx = build_oneshot_context(state.skill_calls)
        fb_reply = (result.fallback_message if result else None) or "未找到匹配的从者。"
        return {
            "result_label": "agent_fallback",
            "extra_query_mode": "agent_fallback",
            "extra_query_extra": None,
            "error_fallback_reply": fb_reply,
            "error_query": {"mode": "execution_fallback"},
            "error_result": "agent_fallback",
            "error_mode": "agent_fallback",
            "error_model": None,
            "oneshot_context": oneshot_ctx,
        }

    # 兜底（不应到达）
    return {
        "result_label": "unknown_bail_out",
        "extra_query_mode": "unknown_bail_out",
        "extra_query_extra": {"reason": reason},
        "error_fallback_reply": "抱歉，处理你的请求时出现了问题，请稍后重试。",
        "error_query": {"mode": "unknown_bail_out", "reason": reason},
        "error_result": "fallback",
        "error_mode": "fallback_no_match",
        "error_model": "error",
        "oneshot_context": None,
    }


@with_trace(Phase.NODE_AGENT)
async def agent_fallback_node(state: PipelineState) -> PipelineState:
    """统一 Agent 兜底节点（4 处合 1）。

    根据 state.extras["bail_out"] 选择对应的 trace 标签和错误兜底配置，
    调用 ``agent_route`` 走 Agent ReAct，把结果写回 state.reply / servants /
    count / query / model_used。
    """
    cfg = _build_agent_config(state)
    trace_id = state.trace_id
    request_start = state.request_start

    # 执行层 fallback 在原代码中调用 agent_route 时附带 oneshot_context
    oneshot_ctx = cfg["oneshot_context"]

    try:
        if oneshot_ctx is not None:
            agent_result: AgentResult = await agent_route(
                state.user_message, TOOL_HANDLERS, trace_id, oneshot_context=oneshot_ctx
            )
        else:
            agent_result = await agent_route(state.user_message, TOOL_HANDLERS, trace_id)

        state.trace_total_tokens += agent_result.total_tokens
        category, clean_reply = classify_agent_reply(agent_result.reply)
        returned = agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []

        # ── Trace: agent_detail ──
        agent_detail_payload: dict = {
            "rounds": agent_result.rounds,
            "agent_tokens": agent_result.total_tokens,
            "tool_trace": agent_result.tool_trace,
            "reply": clean_reply,
        }
        # 执行层 fallback 在原代码中额外记录了 elapsed_ms
        if oneshot_ctx is not None:
            agent_detail_payload["agent_elapsed_ms"] = round(agent_result.elapsed_ms, 2)
        await log_trace_event(trace_id, "agent_detail", agent_detail_payload)

        # ── BI 维度回填（agent fallback 成功路径）──
        state.metric_labels.update(
            {
                "model": f"agent_{agent_result.rounds}r",
                "total_tokens": int(state.trace_total_tokens),
                "pipeline": "agent",
            }
        )

        # ── Trace: final ──
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": cfg["result_label"],
                "mode": "agent_fallback",
                "total_found": len(agent_result.servants_data),
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
        )

        query_payload: dict = {"mode": cfg["extra_query_mode"]}
        if cfg["extra_query_extra"]:
            query_payload.update(cfg["extra_query_extra"])

        state.reply = clean_reply
        state.servants = returned
        state.count = len(agent_result.servants_data)
        state.query = query_payload
        state.model_used = f"agent_{agent_result.rounds}r"
        return state

    except Exception as agent_err:  # noqa: BLE001
        # ── BI 维度回填（agent fallback 失败路径）──
        state.metric_labels.update(
            {
                "error_reason": cfg["error_result"],
                "pipeline": "agent",
            }
        )
        # ── Trace: final（失败）──
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": cfg["error_result"],
                "mode": cfg["error_mode"],
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
            error=str(agent_err) if cfg["error_result"] == "routing_error" else None,
        )
        state.reply = cfg["error_fallback_reply"]
        state.servants = []
        state.count = 0
        state.query = cfg["error_query"]
        if cfg["error_model"] is not None:
            state.model_used = cfg["error_model"]
        return state


# Agent 工具名 → 用户友好中文描述（与 pipeline._AGENT_TOOL_DISPLAY 一致）
_AGENT_TOOL_DISPLAY = {
    "search_servants": "搜索从者",
    "lookup_servant": "查询从者详情",
    "compare_servants": "对比从者",
    "list_effects": "查询效果列表",
    "list_traits": "查询特性列表",
    "list_classes": "查询职阶列表",
    "lookup_skill_detail": "查询技能数值",
}


def _build_agent_progress_messages(tool_trace: list[dict]) -> list[str]:
    """将 Agent tool_trace 转换为用户友好的中文进度消息列表。"""
    messages: list[str] = []
    for entry in tool_trace:
        tool_name = entry.get("tool", "")
        display_name = _AGENT_TOOL_DISPLAY.get(tool_name, tool_name)
        summary = entry.get("result_summary", "")
        messages.append(f"{display_name}：{summary}" if summary else display_name)
    return messages


def _agent_thinking_message(reason: str) -> str:
    """根据 bail_out 原因返回 Agent fallback 入口的 thinking message（与原 SSE 文案对齐）。"""
    if reason == "low_confidence_agent":
        return "正在启动智能搜索..."
    if reason == "routing_failed":
        return "路由异常，正在启动智能搜索..."
    return "需要更深入分析，启动智能搜索..."


async def agent_fallback_stream_node(state: PipelineState) -> AsyncGenerator[dict | PipelineState, None]:
    """Agent 兜底节点（SSE 流式版） — 与 ``agent_fallback_node`` 行为等价，差异仅在 yield 进度事件。

    yield 顺序：
    1. ``thinking phase=agent_fallback`` 入口提示（按 reason 选择文案）
    2. agent_route 完成后：``thinking phase=agent_tool`` * N（每个工具调用一条）
    3. 有从者数据时：``servants`` 事件
    4. ``delta`` 事件（一次性，agent reply 不能逐 token 流）
    5. yield 终态 state
    """
    cfg = _build_agent_config(state)
    reason = state.extras.get("bail_out", "unknown")
    trace_id = state.trace_id
    request_start = state.request_start
    oneshot_ctx = cfg["oneshot_context"]

    yield {
        "type": "thinking",
        "data": {"phase": "agent_fallback", "message": _agent_thinking_message(reason)},
    }

    try:
        if oneshot_ctx is not None:
            agent_result: AgentResult = await agent_route(
                state.user_message, TOOL_HANDLERS, trace_id, oneshot_context=oneshot_ctx
            )
        else:
            agent_result = await agent_route(state.user_message, TOOL_HANDLERS, trace_id)

        state.trace_total_tokens += agent_result.total_tokens
        category, clean_reply = classify_agent_reply(agent_result.reply)
        returned = agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []

        # 推送工具进度
        for progress_msg in _build_agent_progress_messages(agent_result.tool_trace):
            yield {"type": "thinking", "data": {"phase": "agent_tool", "message": progress_msg}}

        # 推送卡片
        if agent_result.servants_data and not category:
            yield {
                "type": "servants",
                "data": {
                    "servants": returned,
                    "count": len(returned),
                    "total": len(agent_result.servants_data),
                },
            }

        # 推送 reply（一次性 delta）
        yield {"type": "delta", "data": {"text": clean_reply}}

        agent_detail_payload: dict = {
            "rounds": agent_result.rounds,
            "agent_tokens": agent_result.total_tokens,
            "tool_trace": agent_result.tool_trace,
            "reply": clean_reply,
        }
        if oneshot_ctx is not None:
            agent_detail_payload["agent_elapsed_ms"] = round(agent_result.elapsed_ms, 2)
        await log_trace_event(trace_id, "agent_detail", agent_detail_payload)

        # ── BI 维度回填（agent fallback stream 成功路径）──
        state.metric_labels.update(
            {
                "model": f"agent_{agent_result.rounds}r",
                "total_tokens": int(state.trace_total_tokens),
                "pipeline": "agent",
            }
        )

        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": cfg["result_label"],
                "mode": "agent_fallback",
                "total_found": len(agent_result.servants_data),
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
        )

        query_payload: dict = {"mode": cfg["extra_query_mode"]}
        if cfg["extra_query_extra"]:
            query_payload.update(cfg["extra_query_extra"])

        state.reply = clean_reply
        state.servants = returned
        state.count = len(agent_result.servants_data)
        state.query = query_payload
        state.model_used = f"agent_{agent_result.rounds}r"
        yield state
        return

    except Exception as agent_err:  # noqa: BLE001
        # ── BI 维度回填（agent fallback stream 失败路径）──
        state.metric_labels.update(
            {
                "error_reason": cfg["error_result"],
                "pipeline": "agent",
            }
        )
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": cfg["error_result"],
                "mode": cfg["error_mode"],
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
            error=str(agent_err) if cfg["error_result"] == "routing_error" else None,
        )
        state.reply = cfg["error_fallback_reply"]
        state.servants = []
        state.count = 0
        state.query = cfg["error_query"]
        if cfg["error_model"] is not None:
            state.model_used = cfg["error_model"]
        # 兜底：确保前端有内容
        try:
            yield {"type": "delta", "data": {"text": state.reply}}
        except Exception:  # noqa: BLE001
            pass
        yield state
