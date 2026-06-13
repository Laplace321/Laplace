"""Pipeline A 降级节点 — Clarification（routing + execution — ADR-028 Task 3）。

迁移自 ``server/pipeline.py`` 的 ``_bail_out_to_chat_response`` 中的两个 clarification 分支：

- ``clarification``           Stage 1 路由判定为需要用户澄清（routing_result.clarification）
- ``execution_clarification`` SkillExecutor 返回 clarification（多候选/猜测后仍歧义）

节点行为：写 trace 事件 + 把 clarification 数据组装到 state.query，作为终态节点连 END。
不调用 LLM，纯数据组装。
"""

from __future__ import annotations

import time

from server.graph.state import PipelineState
from server.logger import log_trace_event


async def clarify_node(state: PipelineState) -> PipelineState:
    """澄清提示节点：把 routing/execution 层的 clarification 数据写入 state.query。"""
    reason = state.extras.get("bail_out", "unknown")
    trace_id = state.trace_id
    request_start = state.request_start

    if reason == "clarification":
        routing_result = state.extras.get("routing_result", {}) or {}
        clarification = routing_result.get("clarification", {}) or {}
        await log_trace_event(
            trace_id,
            "clarification_requested",
            {
                "question": clarification.get("question", ""),
                "options": clarification.get("options", []),
                "ambiguous_field": clarification.get("ambiguous_field", ""),
            },
        )
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": "clarification_requested",
                "mode": "clarification",
                "total_tokens": state.trace_total_tokens,
            },
        )
        state.reply = ""
        state.servants = []
        state.count = 0
        state.query = {"mode": "clarification", "clarification": clarification}
        return state

    # execution_clarification
    result = state.extras.get("executor_result")
    clarification = (result.clarification if result else {}) or {}
    await log_trace_event(
        trace_id,
        "execution_clarification_requested",
        {
            "type": clarification.get("type", ""),
            "question": clarification.get("question", ""),
            "options": clarification.get("options", []),
            "ambiguous_field": clarification.get("ambiguous_field", ""),
        },
    )
    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": (time.monotonic() - request_start) * 1000,
            "result": "execution_clarification_requested",
            "mode": "clarification",
            "total_tokens": state.trace_total_tokens,
        },
    )
    state.reply = ""
    state.servants = []
    state.count = 0
    state.query = {
        "mode": "clarification",
        "clarification": {
            "question": clarification.get("question", ""),
            "options": clarification.get("options", []),
            "ambiguous_field": clarification.get("ambiguous_field", ""),
        },
        "source": "execution",
    }
    return state
