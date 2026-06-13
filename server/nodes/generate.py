"""Pipeline A 生成节点 — RAG 上下文构建 + LLM 生成。

迁移自 ``server/pipeline.py`` 的 RAG 段（``# 执行 Skills`` 之后的 generation 部分）。
本节点行为与原代码等价：
- 根据 response_skill_name / custom_context 选择 context 构建策略
- 使用 Response Skill 的 prompt（如果可用）或默认 generation_prompt
- 调用 chat_completion (非流式) 生成回复，失败时降级为模板文案
- 写 state.reply / state.servants / state.count / state.query / state.model_used

Task 4 Batch B：节点末尾若 ``state.extras["session_store"]`` 与 ``state.session_id`` 同时存在，
则把本轮关键字段封装为 TurnSnapshot 写入 SessionStore，供下一轮分类器加载 prev_summary。

Note: 本节点对应 JSON 模式下的 handle_skill_mode；SSE 流式版本在 Task 5 完善。
"""

from __future__ import annotations

import json
import time

from server.context_builder import MAX_RESULTS, build_ce_context, build_context
from server.graph.session import PREV_SUMMARY_MAX_CHARS, SessionStore, TurnSnapshot
from server.graph.state import PipelineState
from server.llm import chat_completion
from server.logger import log_trace_event
from server.prompts import get_generation_prompt
from server.translation import describe_filters


async def generate_node(state: PipelineState) -> PipelineState:
    """RAG 生成：组装 context → LLM 调用 → 写入 state.reply / query。"""
    result = state.extras.get("executor_result")
    if result is None:
        # 防御性兜底：execute_node 未运行时不应到达此处；保底返回模板
        state.reply = "暂时无法生成回复，请稍后重试。"
        state.query = {"mode": "skill", "skill_calls": state.skill_calls}
        return state

    skill_calls = state.skill_calls
    response_skill_name = state.response_skill_name
    servants = result.servants
    total_found = result.total_found
    returned_servants = servants[:MAX_RESULTS]

    # ── 构建 context ──
    applied_filters = describe_filters(skill_calls)
    if result.custom_context:
        context_data = {
            "知识数据": result.custom_context,
            "已应用的筛选条件": applied_filters,
            "关联从者数量": total_found,
        }
    elif response_skill_name == "respond_ce_list":
        context_data, _ = build_ce_context(servants, skill_calls=skill_calls)
        context_data["已应用的筛选条件"] = applied_filters
        context_data["筛选条件"] = applied_filters
    else:
        detail_mode = response_skill_name in (
            "respond_servant_detail",
            "respond_support_analysis",
            "respond_servant_compare",
        )
        context_data, _ = build_context(servants, detail_mode=detail_mode, skill_calls=skill_calls)
        context_data["已应用的筛选条件"] = applied_filters
        context_data["筛选条件"] = applied_filters

    context_json = json.dumps(context_data, ensure_ascii=False)

    # ── Trace: context_build ──
    await log_trace_event(
        state.trace_id,
        "context_build",
        {
            "applied_filters": applied_filters,
            "context_data": context_data,
        },
    )

    # 使用 Response Skill 的 prompt（如果可用）
    if result.response_skill is not None:
        gen_prompt = result.response_skill.build_prompt(state.user_message, context_json)
    else:
        gen_prompt = get_generation_prompt(state.user_message, context_json)

    # ── Trace: generation_input ──
    await log_trace_event(
        state.trace_id,
        "generation_input",
        {"generation_prompt": gen_prompt},
    )

    final_reply = ""
    try:
        gen_response = await chat_completion(
            system_prompt=(
                "You are a helpful AI assistant. You MUST strictly follow "
                "the provided data and NEVER use your internal knowledge about FGO."
            ),
            user_message=gen_prompt,
            temperature=0.1,
            json_mode=False,
        )
        final_reply = gen_response.get("text", "").strip()
        gen_usage = gen_response.get("_usage", {})
        state.trace_total_tokens += gen_usage.get("total_tokens", 0)
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as gen_err:  # noqa: BLE001
        final_reply = (
            f"为你找到了 {total_found} 个礼装。"
            if response_skill_name == "respond_ce_list"
            else (
                f"为你找到了 {total_found} 位从者。" if not result.custom_context else "暂时无法生成回复，请稍后重试。"
            )
        )
        await log_trace_event(
            state.trace_id,
            "generation_output",
            {"reply": final_reply},
            error=str(gen_err),
        )
    else:
        await log_trace_event(
            state.trace_id,
            "generation_output",
            {"reply": final_reply, "generation_usage": gen_usage},
        )

    # ── Trace: final ──
    await log_trace_event(
        state.trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
            "total_found": total_found,
            "result": "success",
            "mode": "oneshot",
            "total_tokens": state.trace_total_tokens,
        },
    )

    query_info: dict = {"mode": "skill", "skill_calls": skill_calls}
    if result.rejected_skills:
        query_info["rejected_skills"] = result.rejected_skills

    state.reply = final_reply
    state.servants = returned_servants
    state.count = total_found
    state.query = query_info

    # ── Task 4 Batch B：把本轮快照写入 SessionStore，供下一轮 classifier 注入 prev_summary ──
    session_store: SessionStore | None = state.extras.get("session_store")
    if session_store is not None and state.session_id:
        # summary：截断 reply 作为对下一轮分类器的精简摘要（≤200 char）。
        # 保留筛选条件简介，便于 LLM 理解延续语境。
        applied_brief = applied_filters or "（无筛选条件）"
        raw_summary = f"上一轮：{applied_brief}；命中 {total_found} 条；回复要点：{final_reply}"
        if len(raw_summary) > PREV_SUMMARY_MAX_CHARS:
            raw_summary = raw_summary[: PREV_SUMMARY_MAX_CHARS - 1] + "…"
        try:
            snapshot = TurnSnapshot(
                session_id=state.session_id,
                user_message=state.user_message,
                reply=final_reply,
                summary=raw_summary,
                pipeline=state.classified_pipeline or "A",
                skill_calls=list(skill_calls or []),
                response_skill_name=response_skill_name or "respond_servant_list",
                servants=[
                    {"collectionNo": s.get("collectionNo"), "name": s.get("name", "")}
                    for s in returned_servants
                    if isinstance(s, dict)
                ],
                query=query_info,
                turn_type=state.turn_type or "MAJOR",
                timestamp=time.time(),
            )
            session_store.save_turn(snapshot)
        except Exception as save_err:  # noqa: BLE001
            # 写快照失败不应影响本轮回复；仅记录日志
            await log_trace_event(
                state.trace_id,
                "session_save_turn_failed",
                {"error": str(save_err)},
            )

    return state
