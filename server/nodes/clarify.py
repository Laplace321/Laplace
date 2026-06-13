"""Pipeline A 降级节点 — Clarification（routing + execution — ADR-028 Task 3）。

迁移自 ``server/pipeline.py`` 的 ``_bail_out_to_chat_response`` 中的两个 clarification 分支：

- ``clarification``           Stage 1 路由判定为需要用户澄清（routing_result.clarification）
- ``execution_clarification`` SkillExecutor 返回 clarification（多候选/猜测后仍歧义）

节点行为：写 trace 事件 + 把 clarification 数据组装到 state.query，作为终态节点连 END。
不调用 LLM，纯数据组装。

Task 4 Batch B 多轮对话：
- 当 ``state.extras["session_store"]`` 与 ``state.session_id`` 同时存在，调用 ``save_pending``
  把当前 PipelineState 持久化为 pending checkpoint，供后续 ``/chat/resume`` 恢复执行。
- 在 ``state.query`` 中增加 ``pending=True`` 标记，前端可据此区分「需要用户回答的系统中断」
  与「服务端给出的最终答复」。SSE 流式版本（Task 5）会改造为 ``pending_question`` 事件。
"""

from __future__ import annotations

import time

from server.graph.session import SessionStore
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
        await _maybe_save_pending(state, source="routing")
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
    await _maybe_save_pending(state, source="execution")
    return state


async def _maybe_save_pending(state: PipelineState, *, source: str) -> None:
    """有 session_store + session_id 时，把 state 写入 pending checkpoint，并在 query 中打标。

    保存失败不影响主流程（仅记录日志）；前端见 ``query.pending=True`` 即可调用 ``/chat/resume``。
    """
    session_store: SessionStore | None = state.extras.get("session_store")
    if session_store is None or not state.session_id:
        return
    # 持久化 PipelineState 前清掉运行时句柄（session_store 等不可 pickle 的对象）
    snapshot_extras = {
        k: v
        for k, v in state.extras.items()
        if k
        not in {
            "session_store",
            "executor_result",  # SkillExecutor 内部对象，跨进程恢复价值低
            "prev_turn",  # 已落盘到 turn:sid 命名空间，无需重复
        }
    }
    pending_state = PipelineState(
        user_message=state.user_message,
        trace_id=state.trace_id,
        request_start=state.request_start,
        client_ip=state.client_ip,
        classified_pipeline=state.classified_pipeline,
        classifier_confidence=state.classifier_confidence,
        classifier_model=state.classifier_model,
        atlas_query=state.atlas_query,
        skill_calls=list(state.skill_calls or []),
        response_skill_name=state.response_skill_name,
        target_pipeline=state.target_pipeline,
        model_used=state.model_used,
        trace_total_tokens=state.trace_total_tokens,
        session_id=state.session_id,
        turn_type=state.turn_type,
        reply=state.reply,
        servants=list(state.servants or []),
        count=state.count,
        query=state.query,
        extras=snapshot_extras,
    )
    try:
        session_store.save_pending(state.session_id, pending_state)
    except Exception as err:  # noqa: BLE001
        await log_trace_event(
            state.trace_id,
            "session_save_pending_failed",
            {"error": str(err), "source": source},
        )
        return
    # 在 state.query 中打 pending 标记，前端据此识别「系统主动中断」
    if isinstance(state.query, dict):
        state.query["pending"] = True
        state.query["session_id"] = state.session_id
    await log_trace_event(
        state.trace_id,
        "pending_question_saved",
        {"source": source, "session_id": state.session_id},
    )
