"""Pipeline A 入口节点 — Stage 0 链路分类器（ADR-024 / ADR-028）。

迁移自 ``server/pipeline.py`` 的 Stage 0 分类逻辑。本节点行为与原代码完全等价：
- 调用 ``build_classifier_prompt`` 提示词，最多 2 次 LLM 重试
- 把分类结果写回 state.classified_pipeline / classifier_confidence / classifier_model
- 累计 trace_total_tokens

降级行为：
- 2 次重试均失败 → 视为 A 链路 + 1.0 置信度（兼容旧逻辑），由 ``edges.after_classify`` 决定后续路径

Task 4 Batch B 多轮对话扩展：
- 入口加载 ``state.extras["session_store"]`` 中的上一轮 TurnSnapshot，注入 prev_summary
- 解析 LLM 输出的 ``turn_type``，写入 ``state.turn_type``
- ``turn_type == "MAJOR"`` 时清空 session（清除上一轮 turn 历史 + pending），避免标记位残留（ADR-026 教训）
- ``state.session_id`` 为空时跳过多轮逻辑，行为与单轮完全一致
"""

from __future__ import annotations

from server.graph.session import SessionStore
from server.graph.state import PipelineState
from server.llm import chat_completion
from server.logger import log_trace_event
from server.prompts import build_classifier_prompt
from server.schemas import classifier_response_json_schema, parse_classifier_response


async def classify_node(state: PipelineState) -> PipelineState:
    """Stage 0：链路分类（A=Skill 查询 / B=Atlas 知识 / C=攻略文档）。

    输入：state.user_message / state.trace_id / state.session_id /
          state.extras["session_store"]（可选，用于多轮对话）
    输出：state.classified_pipeline / state.classifier_confidence / state.classifier_model /
          state.turn_type / state.trace_total_tokens

    多轮对话副作用：
    - 加载上一轮 TurnSnapshot 并注入 prev_summary 到分类器 prompt
    - 把 prev_turn 写到 state.extras["prev_turn"]，供下游 merge_filters 节点使用
    - turn_type == MAJOR 时调用 SessionStore.clear_session() 清空遗留状态
    """
    # ── SSE：入口 thinking 事件（流式模式才注入）──
    if state.extras.get("streaming"):
        state.pending_events.append(
            {"type": "thinking", "data": {"phase": "routing", "message": "正在分析问题类型..."}}
        )

    # ── 多轮：加载上一轮快照 ──
    session_store: SessionStore | None = state.extras.get("session_store")
    prev_summary: str | None = None
    if session_store is not None and state.session_id:
        prev_turn = session_store.load_prev_turn(state.session_id)
        if prev_turn is not None:
            state.extras["prev_turn"] = prev_turn
            prev_summary = prev_turn.truncated_summary()

    classifier_prompt = build_classifier_prompt(prev_summary=prev_summary)
    classifier_result = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            classifier_result = await chat_completion(
                system_prompt=classifier_prompt,
                user_message=state.user_message,
                temperature=0.0,
                json_mode=True,
                response_schema=classifier_response_json_schema,
                response_validator=parse_classifier_response,
            )
            break
        except Exception as cls_err:  # noqa: BLE001
            last_error = cls_err
            if attempt == 0:
                print(f"⚠️ [{state.trace_id}] Stage 0 分类第 1 次尝试失败，重试中: {cls_err}")

    # Stage 0 失败 → 降级走 A 全量路由（兼容旧逻辑：高置信度 1.0 让后续 route_node 处理）
    if classifier_result is None:
        print(f"⚠️ [{state.trace_id}] Stage 0 分类 2 次均失败，降级全量路由: {last_error}")
        state.classified_pipeline = "A"
        state.classifier_confidence = 1.0
        state.classifier_model = "unknown"
        state.turn_type = "MAJOR"
        # 多轮防御：分类失败时也按 MAJOR 处理，主动清状态避免污染
        if session_store is not None and state.session_id:
            session_store.clear_session(state.session_id)
        return state

    classifier_model = classifier_result.pop("_model", "unknown")
    classifier_result.pop("_response_format", None)
    classifier_result.pop("_provider", None)
    classifier_result.pop("_attempts", None)
    classifier_usage = classifier_result.pop("_usage", {})
    state.trace_total_tokens += classifier_usage.get("total_tokens", 0)
    state.classified_pipeline = classifier_result.get("pipeline", "A")
    state.classifier_confidence = classifier_result.get("confidence", 0.0)
    state.classifier_model = classifier_model

    # turn_type 解析（schema 默认 MAJOR；无 prev_turn 强制改回 MAJOR 防御 LLM 误判）
    turn_type = classifier_result.get("turn_type", "MAJOR") or "MAJOR"
    if turn_type not in ("MAJOR", "MINOR", "CORRECTION"):
        turn_type = "MAJOR"
    if "prev_turn" not in state.extras and turn_type != "MAJOR":
        # 没有上一轮上下文却判 MINOR/CORRECTION 是 LLM 漂移，强制纠正为 MAJOR
        turn_type = "MAJOR"
    state.turn_type = turn_type

    # ── 多轮副作用：MAJOR 时清空 session ──
    if turn_type == "MAJOR" and session_store is not None and state.session_id:
        session_store.clear_session(state.session_id)
        # 已清的 prev_turn 也从 extras 中移除，下游不应再使用
        state.extras.pop("prev_turn", None)

    await log_trace_event(
        state.trace_id,
        "classifier_output",
        {
            "pipeline": state.classified_pipeline,
            "confidence": state.classifier_confidence,
            "turn_type": state.turn_type,
            "has_prev_turn": "prev_turn" in state.extras,
            "model": classifier_model,
            "usage": classifier_usage,
        },
    )

    return state
