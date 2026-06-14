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
from collections.abc import AsyncGenerator

from server.context_builder import MAX_RESULTS, build_ce_context, build_context, build_servant_brief
from server.graph.decorators import with_trace
from server.graph.session import PREV_SUMMARY_MAX_CHARS, SessionStore, TurnSnapshot
from server.graph.state import PipelineState
from server.llm import StreamMetadata, chat_completion, chat_completion_stream
from server.logger import Phase, log_trace_event
from server.prompts import get_generation_prompt
from server.translation import describe_filters


def _latency_bucket(total_ms: float) -> str:
    """将耗时映射为有限桶，控制 BI label 基数。"""
    if total_ms < 200:
        return "<200ms"
    if total_ms < 500:
        return "200-500ms"
    if total_ms < 1000:
        return "500-1000ms"
    if total_ms < 3000:
        return "1000-3000ms"
    if total_ms < 5000:
        return "3000-5000ms"
    return ">=5000ms"


#: 当 returned_servants 数量不大于该值时，为本轮生成 servant_briefs。
#: 超过则视为列表场景（如「五星弓阶」），仅依赖 servants 名字字段做下一轮锚定。
_BRIEF_MAX_SERVANTS = 3


def _compute_servant_briefs(state: PipelineState, returned_servants: list[dict]) -> list[str]:
    """根据本轮命中情况决定是否生成 servant_briefs（ADR-031 / Phase 1）。

    命中条件（全部满足才生成）：
    1. state.classified_pipeline == "A"（B/C 链路当前不写 turn，无需此字段）
    2. ``len(returned_servants) <= _BRIEF_MAX_SERVANTS``（避免列表场景污染 token 预算）
    3. returned_servants 全部为 dict
    """
    if state.classified_pipeline != "A":
        return []
    if not returned_servants or len(returned_servants) > _BRIEF_MAX_SERVANTS:
        return []
    briefs: list[str] = []
    for s in returned_servants:
        if not isinstance(s, dict):
            continue
        text = build_servant_brief(s)
        if text:
            briefs.append(text)
    return briefs


@with_trace(Phase.NODE_GENERATE)
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
    gen_usage: dict = {}
    gen_model: str = "unknown"
    final_result = "success"
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
        gen_model = gen_response.get("_model", "unknown")
        state.trace_total_tokens += gen_usage.get("total_tokens", 0)
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as gen_err:  # noqa: BLE001
        final_result = "generation_error"
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

    # ── BI 维度回填 ──
    total_time_ms = round((time.monotonic() - state.request_start) * 1000, 2)
    state.metric_labels.update(
        {
            "model": gen_model,
            "total_tokens": int(state.trace_total_tokens),
            "latency_bucket": _latency_bucket(total_time_ms),
        }
    )

    # ── Trace: final ──
    await log_trace_event(
        state.trace_id,
        "final",
        {
            "total_time_ms": total_time_ms,
            "total_found": total_found,
            "result": final_result,
            "mode": "oneshot",
            "total_tokens": state.trace_total_tokens,
            "metric_labels": dict(state.metric_labels),
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
                servant_briefs=_compute_servant_briefs(state, returned_servants),
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


async def generate_stream_node(state: PipelineState) -> AsyncGenerator[dict | PipelineState, None]:
    """RAG 生成（SSE 流式版） — 与 ``generate_node`` 行为等价，差异仅在 LLM 流式 yield delta。

    yield 顺序：
    1. ``servants`` 事件（卡片先行，如有）
    2. ``thinking phase=generating`` 事件
    3. ``delta`` 事件 * N（chat_completion_stream 逐 chunk）
    4. yield 最终 state（引擎检测到非事件 dict 时视为新 state）

    生成失败时仍 yield 一次性兜底 delta，保证前端有内容；trace + save_turn 行为与非流式版一致。
    """
    result = state.extras.get("executor_result")
    if result is None:
        # 防御性兜底：execute_node 未运行时不应到达此处
        state.reply = "暂时无法生成回复，请稍后重试。"
        state.query = {"mode": "skill", "skill_calls": state.skill_calls}
        yield {"type": "delta", "data": {"text": state.reply}}
        yield state
        return

    skill_calls = state.skill_calls
    response_skill_name = state.response_skill_name
    servants = result.servants
    total_found = result.total_found
    returned_servants = servants[:MAX_RESULTS]

    # 卡片先行（与 stream_event_generator 行为对齐）
    if returned_servants:
        yield {
            "type": "servants",
            "data": {
                "servants": returned_servants,
                "count": len(returned_servants),
                "total": total_found,
            },
        }

    # ── 构建 context（与 generate_node 完全一致）──
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

    await log_trace_event(
        state.trace_id,
        "context_build",
        {"applied_filters": applied_filters, "context_data": context_data},
    )

    if result.response_skill is not None:
        gen_prompt = result.response_skill.build_prompt(state.user_message, context_json)
    else:
        gen_prompt = get_generation_prompt(state.user_message, context_json)

    await log_trace_event(
        state.trace_id,
        "generation_input",
        {"generation_prompt": gen_prompt},
    )

    yield {"type": "thinking", "data": {"phase": "generating", "message": "正在生成分析..."}}

    # ── LLM 流式调用 ──
    full_reply_parts: list[str] = []
    final_result = "success"
    gen_usage: dict = {}
    gen_model: str = "unknown"
    final_reply = ""
    try:
        stream_metadata = StreamMetadata()
        async for chunk in chat_completion_stream(
            system_prompt=(
                "You are a helpful AI assistant. You MUST strictly follow "
                "the provided data and NEVER use your internal knowledge about FGO."
            ),
            user_message=gen_prompt,
            temperature=0.1,
            max_tokens=2048,
            metadata=stream_metadata,
        ):
            full_reply_parts.append(chunk)
            yield {"type": "delta", "data": {"text": chunk}}

        final_reply = "".join(full_reply_parts).strip()
        gen_usage = stream_metadata.usage
        gen_model = stream_metadata.model or "unknown"
        state.trace_total_tokens += gen_usage.get("total_tokens", 0)
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as gen_err:  # noqa: BLE001
        # 失败兜底：与原 stream_event_generator 行为一致 — 已有内容则保留，否则 yield 模板
        if full_reply_parts:
            final_reply = "".join(full_reply_parts).strip()
        else:
            final_reply = (
                f"为你找到了 {total_found} 个礼装。"
                if response_skill_name == "respond_ce_list"
                else (
                    f"为你找到了 {total_found} 位从者。"
                    if not result.custom_context
                    else "暂时无法生成回复，请稍后重试。"
                )
            )
            final_result = "generation_error"
            try:
                yield {"type": "delta", "data": {"text": final_reply}}
            except Exception:  # noqa: BLE001
                final_result = "client_disconnected"
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

    # ── BI 维度回填（流式版）──
    total_time_ms = round((time.monotonic() - state.request_start) * 1000, 2)
    state.metric_labels.update(
        {
            "model": gen_model,
            "total_tokens": int(state.trace_total_tokens),
            "latency_bucket": _latency_bucket(total_time_ms),
        }
    )

    await log_trace_event(
        state.trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
            "total_found": total_found,
            "result": final_result,
            "mode": "oneshot",
            "total_tokens": state.trace_total_tokens,
            "metric_labels": dict(state.metric_labels),
        },
    )

    query_info: dict = {"mode": "skill", "skill_calls": skill_calls}
    if result.rejected_skills:
        query_info["rejected_skills"] = result.rejected_skills

    state.reply = final_reply
    state.servants = returned_servants
    state.count = total_found
    state.query = query_info

    # ── 多轮：写 TurnSnapshot（与 generate_node 一致）──
    session_store: SessionStore | None = state.extras.get("session_store")
    if session_store is not None and state.session_id:
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
                servant_briefs=_compute_servant_briefs(state, returned_servants),
                query=query_info,
                turn_type=state.turn_type or "MAJOR",
                timestamp=time.time(),
            )
            session_store.save_turn(snapshot)
        except Exception as save_err:  # noqa: BLE001
            await log_trace_event(
                state.trace_id,
                "session_save_turn_failed",
                {"error": str(save_err)},
            )

    yield state
