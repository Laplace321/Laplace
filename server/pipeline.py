"""请求处理管线 — OneShot 路由 + SkillExecutor + RAG 生成。

从 main.py 抽取的核心业务处理函数：
- handle_skill_mode — JSON API 的 Skill 模式处理（路由/执行/生成）
- stream_event_generator — SSE 流式端点的事件生成器
"""

from __future__ import annotations

import json
import time
import uuid

# ChatResponse 在 main.py 中定义，这里用 Pydantic BaseModel 重新定义一个兼容类型
# 避免循环导入（pipeline 被 main 导入，main 定义 ChatResponse）
from pydantic import BaseModel

from server.agent.agent_loop import AgentResult, agent_route
from server.agent.tool_handlers import TOOL_HANDLERS
from server.context_builder import MAX_RESULTS, build_ce_context, build_context
from server.fallback import (
    FALLBACK_TEMPLATES,
    build_oneshot_context,
    classify_agent_reply,
    sse_event,
)
from server.llm import StreamMetadata, chat_completion, chat_completion_stream
from server.logger import log_trace_event
from server.prompts import build_classifier_prompt, build_routing_prompt, get_generation_prompt
from server.schemas import (
    classifier_response_json_schema,
    parse_classifier_response,
    parse_routing_response,
    routing_response_json_schema,
)
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.skills.executor import SkillExecutor
from server.skills.presets import PRESET_REGISTRY
from server.translation import describe_filters

# Agent 工具名 → 用户友好中文描述
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
    messages = []
    for entry in tool_trace:
        tool_name = entry.get("tool", "")
        display_name = _AGENT_TOOL_DISPLAY.get(tool_name, tool_name)
        summary = entry.get("result_summary", "")
        messages.append(f"{display_name}：{summary}" if summary else display_name)
    return messages


class ChatResponse(BaseModel):
    """对话响应（pipeline 内部使用，与 main.py 中的定义保持一致）。"""

    reply: str
    servants: list[dict]
    count: int
    query: dict
    model: str
    traceId: str | None = None


async def handle_skill_mode(
    user_message: str,
    trace_id: str,
    skill_calls: list[dict] | None = None,
    response_skill_name: str = "respond_servant_list",
    client_ip: str = "unknown",
    target_pipeline: str = "A",
    confirmation_context: str | None = None,
) -> ChatResponse:
    """Skill 模式的核心处理逻辑。

    接收已确定的 skill_calls（来自 LLM 路由或前端直传），
    执行 SkillExecutor 并生成 RAG 回复。

    Args:
        confirmation_context: 用户确认选择后回传的上下文，拼接到 user_message 进行精确路由。
    """
    # 用户确认后的第二次请求：将选择上下文拼接到消息
    if confirmation_context:
        user_message = f"{user_message}\n[用户确认：{confirmation_context}]"
    request_start = time.monotonic()
    executor = SkillExecutor()
    model_used = "skill_mode"
    trace_mode = "oneshot"  # 追踪最终模式：oneshot | agent_fallback | fallback_*
    trace_total_tokens = 0  # 累计 token 消耗

    # 如果已有 skill_calls（preset 或前端直传），记录 routing 事件
    if skill_calls is not None:
        await log_trace_event(
            trace_id,
            "routing_input",
            {
                "query": user_message,
                "source": "direct",
                "mode": "oneshot_direct",
                "skill_count": len(skill_calls),
                "client_ip": client_ip,
                "target_pipeline": target_pipeline,
            },
        )
        await log_trace_event(
            trace_id,
            "routing_output",
            {
                "skill_calls": skill_calls,
                "response_skill": response_skill_name,
                "fallback": None,
                "model": model_used,
                "target_pipeline": target_pipeline,
            },
        )

    # 如果没有传入 skill_calls，通过两阶段路由获取
    if skill_calls is None:
        skill_descriptions = [
            {"name": s.name, "description": s.description} for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)
        ]

        # ── Trace: routing_input ──
        await log_trace_event(
            trace_id,
            "routing_input",
            {
                "query": user_message,
                "mode": "two_stage",
                "skill_count": len(skill_descriptions),
                "client_ip": client_ip,
                "is_confirmation": confirmation_context is not None,
                "confirmation_context": confirmation_context[:200] if confirmation_context else None,
            },
        )

        try:
            # ══════════════════════════════════════════════════════
            # Stage 0: 链路分类器（ADR-024）
            # ══════════════════════════════════════════════════════
            classifier_prompt = build_classifier_prompt()
            classifier_result = None
            _classifier_error = None
            for _cls_attempt in range(2):
                try:
                    classifier_result = await chat_completion(
                        system_prompt=classifier_prompt,
                        user_message=user_message,
                        temperature=0.0,
                        json_mode=True,
                        response_schema=classifier_response_json_schema,
                        response_validator=parse_classifier_response,
                    )
                    break
                except Exception as cls_err:
                    _classifier_error = cls_err
                    if _cls_attempt == 0:
                        print(f"⚠️ [{trace_id}] Stage 0 分类第 1 次尝试失败，重试中: {cls_err}")

            # Stage 0 失败 → 降级走 Stage 1 全量路由（兼容旧逻辑）
            if classifier_result is None:
                print(f"⚠️ [{trace_id}] Stage 0 分类 2 次均失败，降级全量路由: {_classifier_error}")
                classified_pipeline = "A"
                classifier_confidence = 1.0  # 降级时视为高置信度，让 Stage 1 处理
            else:
                classifier_model = classifier_result.pop("_model", "unknown")
                classifier_result.pop("_response_format", None)
                classifier_result.pop("_provider", None)
                classifier_result.pop("_attempts", None)
                classifier_usage = classifier_result.pop("_usage", {})
                trace_total_tokens += classifier_usage.get("total_tokens", 0)
                classified_pipeline = classifier_result.get("pipeline", "A")
                classifier_confidence = classifier_result.get("confidence", 0.0)

                # ── Trace: classifier_output ──
                await log_trace_event(
                    trace_id,
                    "classifier_output",
                    {
                        "pipeline": classified_pipeline,
                        "confidence": classifier_confidence,
                        "model": classifier_model,
                        "usage": classifier_usage,
                    },
                )

            # ── Stage 0 分发：B/C 链路直接处理 ──
            if classified_pipeline == "B":
                return await _handle_atlas_pipeline(
                    user_message, trace_id, model_used, request_start, trace_total_tokens
                )
            if classified_pipeline == "C":
                return await _handle_guide_pipeline(
                    user_message, trace_id, model_used, request_start, trace_total_tokens
                )

            # ── Stage 0 分发：A 链路低置信度 → Agent fallback ──
            if classified_pipeline == "A" and classifier_confidence < 0.6:
                try:
                    agent_result = await agent_route(user_message, TOOL_HANDLERS, trace_id)
                    trace_total_tokens += agent_result.total_tokens
                    category, clean_reply = classify_agent_reply(agent_result.reply)
                    returned = (
                        agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []
                    )
                    await log_trace_event(
                        trace_id,
                        "agent_detail",
                        {
                            "rounds": agent_result.rounds,
                            "agent_tokens": agent_result.total_tokens,
                            "tool_trace": agent_result.tool_trace,
                            "reply": clean_reply,
                        },
                    )
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "classifier_low_confidence_agent",
                            "mode": "agent_fallback",
                            "classifier_confidence": classifier_confidence,
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=clean_reply,
                        servants=returned,
                        count=len(agent_result.servants_data),
                        query={"mode": "agent_fallback", "classifier_confidence": classifier_confidence},
                        model=f"agent_{agent_result.rounds}r",
                        traceId=trace_id,
                    )
                except Exception:
                    pass  # Agent fallback 失败，继续走 Stage 1

            # ══════════════════════════════════════════════════════
            # Stage 1: Skill 选择 + 参数提取（仅链路 A）
            # ══════════════════════════════════════════════════════
            routing_prompt = build_routing_prompt(skill_descriptions)

            # 路由失败重试：最多 2 次尝试，全部失败走 Agent 兜底
            routing_result = None
            _routing_last_error = None
            for _routing_attempt in range(2):
                try:
                    routing_result = await chat_completion(
                        system_prompt=routing_prompt,
                        user_message=user_message,
                        temperature=0.1,
                        json_mode=True,
                        response_schema=routing_response_json_schema,
                        response_validator=parse_routing_response,
                    )
                    break  # 成功则跳出重试循环
                except Exception as retry_err:
                    _routing_last_error = retry_err
                    if _routing_attempt == 0:
                        print(f"⚠️ [{trace_id}] Stage 1 路由第 1 次尝试失败，重试中: {retry_err}")

            # 2 次路由均失败 → Agent 兜底
            if routing_result is None:
                print(f"⚠️ [{trace_id}] 路由 2 次均失败，降级到 Agent: {_routing_last_error}")
                try:
                    agent_result = await agent_route(user_message, TOOL_HANDLERS, trace_id)
                    trace_total_tokens += agent_result.total_tokens
                    category, clean_reply = classify_agent_reply(agent_result.reply)
                    returned = (
                        agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []
                    )
                    await log_trace_event(
                        trace_id,
                        "agent_detail",
                        {
                            "rounds": agent_result.rounds,
                            "agent_tokens": agent_result.total_tokens,
                            "tool_trace": agent_result.tool_trace,
                            "reply": clean_reply,
                        },
                    )
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "routing_retry_agent_fallback",
                            "mode": "agent_fallback",
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=clean_reply,
                        servants=returned,
                        count=len(agent_result.servants_data),
                        query={"mode": "routing_retry_agent_fallback", "routing_error": str(_routing_last_error)},
                        model=f"agent_{agent_result.rounds}r",
                        traceId=trace_id,
                    )
                except Exception as agent_err:
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "routing_error",
                            "mode": "routing_error",
                            "total_tokens": trace_total_tokens,
                        },
                        error=f"routing: {_routing_last_error}; agent: {agent_err}",
                    )
                    return ChatResponse(
                        reply="抱歉，Skill 路由遇到问题，请稍后重试。",
                        servants=[],
                        count=0,
                        query={},
                        model="error",
                        traceId=trace_id,
                    )

            model_used = routing_result.pop("_model", "unknown")
            routing_result.pop("_response_format", None)
            routing_result.pop("_provider", None)
            routing_result.pop("_attempts", None)
            routing_usage = routing_result.pop("_usage", {})
            trace_total_tokens += routing_usage.get("total_tokens", 0)
            skill_calls = routing_result.get("skill_calls", [])
            response_skill_name = routing_result.get("response_skill", response_skill_name)

            # ── Trace: routing_output ──
            await log_trace_event(
                trace_id,
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
                        "total_tokens": trace_total_tokens,
                    },
                )
                return ChatResponse(
                    reply="",
                    servants=[],
                    count=0,
                    query={
                        "mode": "clarification",
                        "clarification": clarification,
                    },
                    model=model_used,
                    traceId=trace_id,
                )

            # 检查 fallback
            fallback = routing_result.get("fallback")
            if fallback is not None:
                fb_code = fallback.get("code", "no_match")
                fb_msg = fallback.get("message", "无法理解你的问题，请尝试更具体的描述。")
                # greeting / out_of_scope → 直接模板回复
                if fb_code in ("greeting", "out_of_scope"):
                    template_reply = FALLBACK_TEMPLATES.get(fb_code.upper(), fb_msg)
                    trace_mode = f"fallback_{fb_code}"
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": trace_mode,
                            "mode": trace_mode,
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=template_reply,
                        servants=[],
                        count=0,
                        query=routing_result,
                        model=model_used,
                        traceId=trace_id,
                    )
                # no_match / ambiguous → Agent 兜底
                try:
                    agent_result: AgentResult = await agent_route(user_message, TOOL_HANDLERS, trace_id)
                    trace_total_tokens += agent_result.total_tokens
                    category, clean_reply = classify_agent_reply(agent_result.reply)
                    returned = (
                        agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []
                    )
                    await log_trace_event(
                        trace_id,
                        "agent_detail",
                        {
                            "rounds": agent_result.rounds,
                            "agent_tokens": agent_result.total_tokens,
                            "tool_trace": agent_result.tool_trace,
                            "agent_elapsed_ms": round(agent_result.elapsed_ms, 2),
                            "reply": clean_reply,
                        },
                    )
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "agent_fallback",
                            "mode": "agent_fallback",
                            "total_found": len(agent_result.servants_data),
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=clean_reply,
                        servants=returned,
                        count=len(agent_result.servants_data),
                        query={"mode": "agent_fallback"},
                        model=f"agent_{agent_result.rounds}r",
                        traceId=trace_id,
                    )
                except Exception:
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "fallback",
                            "mode": "fallback_no_match",
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=fb_msg,
                        servants=[],
                        count=0,
                        query=routing_result,
                        model=model_used,
                        traceId=trace_id,
                    )

            # 空 skill_calls 且无 fallback → Agent 兜底
            if not skill_calls:
                try:
                    agent_result = await agent_route(user_message, TOOL_HANDLERS, trace_id)
                    trace_total_tokens += agent_result.total_tokens
                    category, clean_reply = classify_agent_reply(agent_result.reply)
                    returned = (
                        agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []
                    )
                    await log_trace_event(
                        trace_id,
                        "agent_detail",
                        {
                            "rounds": agent_result.rounds,
                            "agent_tokens": agent_result.total_tokens,
                            "tool_trace": agent_result.tool_trace,
                            "agent_elapsed_ms": round(agent_result.elapsed_ms, 2),
                            "reply": clean_reply,
                        },
                    )
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "agent_fallback",
                            "mode": "agent_fallback",
                            "total_found": len(agent_result.servants_data),
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=clean_reply,
                        servants=returned,
                        count=len(agent_result.servants_data),
                        query={"mode": "agent_fallback"},
                        model=f"agent_{agent_result.rounds}r",
                        traceId=trace_id,
                    )
                except Exception:
                    no_match_msg = "无法从你的问题中识别出查询条件，请尝试更具体的描述。"
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": (time.monotonic() - request_start) * 1000,
                            "result": "no_match",
                            "mode": "fallback_no_match",
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    return ChatResponse(
                        reply=no_match_msg,
                        servants=[],
                        count=0,
                        query=routing_result,
                        model=model_used,
                        traceId=trace_id,
                    )
        except Exception as e:
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - request_start) * 1000,
                    "result": "routing_error",
                    "mode": "routing_error",
                    "total_tokens": trace_total_tokens,
                },
                error=str(e),
            )
            return ChatResponse(
                reply="抱歉，Skill 路由遇到问题，请稍后重试。",
                servants=[],
                count=0,
                query={},
                model="error",
                traceId=trace_id,
            )

    # 执行 Skills
    result = executor.execute(skill_calls, response_skill_name)
    servants = result.servants
    total_found = result.total_found
    returned_servants = servants[:MAX_RESULTS]

    # ── Trace: execution ──
    await log_trace_event(
        trace_id,
        "execution",
        {
            "accepted_skills": result.accepted_skills,
            "rejected_skills": result.rejected_skills,
            "total_found": total_found,
            "execution_time_ms": round(result.execution_time_ms, 2),
            "is_fallback": result.is_fallback,
            "has_clarification": result.clarification is not None,
        },
    )

    # ── 执行层 clarification 检测 ──
    if result.clarification:
        from server.skills.executor import CLARIFICATION_EMPTY_NAME

        # 名称查询空结果：异步 LLM 猜测填充候选
        if result.clarification.get("type") == CLARIFICATION_EMPTY_NAME:
            result = await executor.guess_candidates_async(result)

        # 猜测后仍有 clarification → 推送给前端
        if result.clarification:
            await log_trace_event(
                trace_id,
                "execution_clarification_requested",
                {
                    "type": result.clarification.get("type", ""),
                    "question": result.clarification.get("question", ""),
                    "options": result.clarification.get("options", []),
                    "ambiguous_field": result.clarification.get("ambiguous_field", ""),
                },
            )
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - request_start) * 1000,
                    "result": "execution_clarification_requested",
                    "mode": "clarification",
                    "total_tokens": trace_total_tokens,
                },
            )
            return ChatResponse(
                reply="",
                servants=[],
                count=0,
                query={
                    "mode": "clarification",
                    "clarification": {
                        "question": result.clarification["question"],
                        "options": result.clarification.get("options", []),
                        "ambiguous_field": result.clarification.get("ambiguous_field", ""),
                    },
                    "source": "execution",
                },
                model=model_used,
                traceId=trace_id,
            )
        # 猜测成功（clarification 被清除），更新结果变量
        servants = result.servants
        total_found = result.total_found
        returned_servants = servants[:MAX_RESULTS]

    # Fallback 处理：先尝试异步昵称识别，再 Agent 兜底
    if result.is_fallback:
        # 异步 resolve_nickname fallback（LLM 昵称识别）
        result = await executor.try_resolve_nickname_async(result, skill_calls)
        if not result.is_fallback:
            # 昵称识别成功，走正常 RAG 生成路径
            servants = result.servants
            total_found = result.total_found
            returned_servants = servants[:MAX_RESULTS]
            # 更新 execution trace（追加 resolve_nickname 信息）
            await log_trace_event(
                trace_id,
                "execution_resolve_nickname",
                {
                    "accepted_skills": result.accepted_skills,
                    "total_found": total_found,
                    "execution_time_ms": round(result.execution_time_ms, 2),
                },
            )
        else:
            # 昵称识别也失败，进入 Agent fallback
            pass

    if result.is_fallback:
        oneshot_ctx = build_oneshot_context(skill_calls)
        try:
            agent_result = await agent_route(user_message, TOOL_HANDLERS, trace_id, oneshot_context=oneshot_ctx)
            trace_total_tokens += agent_result.total_tokens
            category, clean_reply = classify_agent_reply(agent_result.reply)
            returned = agent_result.servants_data[:MAX_RESULTS] if agent_result.servants_data and not category else []
            await log_trace_event(
                trace_id,
                "agent_detail",
                {
                    "rounds": agent_result.rounds,
                    "agent_tokens": agent_result.total_tokens,
                    "tool_trace": agent_result.tool_trace,
                    "agent_elapsed_ms": round(agent_result.elapsed_ms, 2),
                    "reply": clean_reply,
                },
            )
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - request_start) * 1000,
                    "result": "agent_fallback",
                    "mode": "agent_fallback",
                    "total_found": len(agent_result.servants_data),
                    "total_tokens": trace_total_tokens,
                },
            )
            return ChatResponse(
                reply=clean_reply,
                servants=returned,
                count=len(agent_result.servants_data),
                query={"mode": "agent_fallback"},
                model=f"agent_{agent_result.rounds}r",
                traceId=trace_id,
            )
        except Exception:
            final_reply = result.fallback_message or "未找到匹配的从者。"
    else:
        # RAG 生成
        applied_filters = describe_filters(skill_calls)

        # custom_context 路径：知识/配队数据直接作为 context
        if result.custom_context:
            context_data = {
                "知识数据": result.custom_context,
                "已应用的筛选条件": applied_filters,
                "关联从者数量": total_found,
            }
        elif response_skill_name == "respond_ce_list":
            # CE domain：使用礼装专用 context 构建
            context_data, _ = build_ce_context(servants, skill_calls=skill_calls)
            context_data["已应用的筛选条件"] = applied_filters
            context_data["筛选条件"] = applied_filters
        else:
            detail_mode = response_skill_name == "respond_servant_detail"
            context_data, _ = build_context(servants, detail_mode=detail_mode, skill_calls=skill_calls)
            context_data["已应用的筛选条件"] = applied_filters
            context_data["筛选条件"] = applied_filters

        context_json = json.dumps(context_data, ensure_ascii=False)

        # ── Trace: context_build ──
        await log_trace_event(
            trace_id,
            "context_build",
            {
                "applied_filters": applied_filters,
                "context_data": context_data,
            },
        )

        # 使用 Response Skill 的 prompt（如果可用）
        if result.response_skill is not None:
            gen_prompt = result.response_skill.build_prompt(user_message, context_json)
        else:
            gen_prompt = get_generation_prompt(user_message, context_json)

        # ── Trace: generation_input ──
        await log_trace_event(
            trace_id,
            "generation_input",
            {
                "generation_prompt": gen_prompt,
            },
        )

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
            trace_total_tokens += gen_usage.get("total_tokens", 0)
            if not final_reply:
                raise ValueError("Empty response from LLM")
        except Exception as e:
            final_reply = (
                f"为你找到了 {total_found} 个礼装。"
                if response_skill_name == "respond_ce_list"
                else (
                    f"为你找到了 {total_found} 位从者。"
                    if not result.custom_context
                    else "暂时无法生成回复，请稍后重试。"
                )
            )
            await log_trace_event(
                trace_id,
                "generation_output",
                {
                    "reply": final_reply,
                },
                error=str(e),
            )
        else:
            # ── Trace: generation_output (success) ──
            await log_trace_event(
                trace_id,
                "generation_output",
                {
                    "reply": final_reply,
                    "generation_usage": gen_usage,
                },
            )

    # ── Trace: final ──
    trace_mode = "oneshot" if not result.is_fallback else "execution_fallback"
    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - request_start) * 1000, 2),
            "total_found": total_found,
            "result": "success" if not result.is_fallback else "fallback",
            "mode": trace_mode,
            "total_tokens": trace_total_tokens,
        },
    )

    query_info: dict = {"mode": "skill", "skill_calls": skill_calls}
    if result.rejected_skills:
        query_info["rejected_skills"] = result.rejected_skills
    return ChatResponse(
        reply=final_reply,
        servants=returned_servants,
        count=total_found,
        query=query_info,
        model=model_used,
        traceId=trace_id,
    )


async def _handle_confirmation_direct(
    *,
    message: str,
    confirmation_id: int,
    confirmation_context: str,
    trace_id: str,
    stream_start: float,
    client_ip: str,
):
    """确认直达路径：用户通过 clarification 选择了具体实体（collectionNo），
    直接从 DB 定位唯一从者/礼装，跳过 LLM 路由和 Skill 执行，直入 RAG 生成。

    这避免了"选择后重新路由 → 名称模糊匹配又找到多个 → 再次 clarification"的死循环。
    """
    from server.query_executor import load_ce_database, load_database

    model_used = "confirmation_direct"
    trace_total_tokens = 0

    yield sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})

    # ── Trace: confirmation_direct ──
    await log_trace_event(
        trace_id,
        "routing_input",
        {
            "query": message,
            "mode": "confirmation_direct",
            "confirmation_id": confirmation_id,
            "confirmation_context": confirmation_context[:200] if confirmation_context else "",
            "client_ip": client_ip,
        },
    )

    # 按 collectionNo 从两个数据库中查找
    servant_db = load_database()
    target = None
    domain = "servant"
    for servant in servant_db:
        if servant.get("collectionNo") == confirmation_id:
            target = servant
            break

    if target is None:
        ce_db = load_ce_database()
        for ce_item in ce_db:
            if ce_item.get("collectionNo") == confirmation_id:
                target = ce_item
                domain = "ce"
                break

    if target is None:
        yield sse_event("error", {"message": f"未找到编号 {confirmation_id} 的实体"})
        await log_trace_event(
            trace_id,
            "final",
            {"total_time_ms": (time.monotonic() - stream_start) * 1000, "result": "confirmation_not_found"},
        )
        yield sse_event("done", {"model": model_used, "traceId": trace_id})
        return

    servants = [target]
    total_found = 1
    response_skill_name = "respond_ce_list" if domain == "ce" else "respond_servant_detail"
    skill_calls = [
        {
            "skill_name": "lookup_servant" if domain == "servant" else "ce_lookup",
            "params": {"name": confirmation_context},
        }
    ]

    yield sse_event(
        "thinking",
        {"phase": "routed", "message": "意图识别完成", "detail": f"查询从者「{confirmation_context}」"},
    )

    await log_trace_event(
        trace_id,
        "routing_output",
        {"skill_calls": skill_calls, "response_skill": response_skill_name, "mode": "confirmation_direct"},
    )

    # ── Trace: execution ──
    await log_trace_event(
        trace_id,
        "execution",
        {"accepted_skills": skill_calls, "total_found": total_found, "confirmation_direct": True},
    )

    yield sse_event("thinking", {"phase": "querying", "message": "正在检索从者数据..."})

    # 卡片先行
    returned_servants = servants[:MAX_RESULTS]
    if returned_servants:
        yield sse_event(
            "servants",
            {"servants": returned_servants, "count": len(returned_servants), "total": total_found},
        )

    # ── RAG 生成 ──
    yield sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

    applied_filters = describe_filters(skill_calls)

    if domain == "ce":
        context_data, _ = build_ce_context(servants, skill_calls=skill_calls)
        context_data["已应用的筛选条件"] = applied_filters
        context_data["筛选条件"] = applied_filters
    else:
        detail_mode = response_skill_name == "respond_servant_detail"
        context_data, _ = build_context(servants, detail_mode=detail_mode, skill_calls=skill_calls)
        context_data["已应用的筛选条件"] = applied_filters
        context_data["筛选条件"] = applied_filters

    context_json = json.dumps(context_data, ensure_ascii=False)

    await log_trace_event(trace_id, "context_build", {"applied_filters": applied_filters, "context_data": context_data})

    # 使用 Response Skill 的 prompt（如果可用）
    response_skill = SKILL_REGISTRY.get(response_skill_name)
    from server.skills.base import ResponseSkill

    if response_skill is not None and isinstance(response_skill, ResponseSkill):
        gen_prompt = response_skill.build_prompt(message, context_json)
    else:
        gen_prompt = get_generation_prompt(message, context_json)

    await log_trace_event(trace_id, "generation_input", {"generation_prompt": gen_prompt})

    full_reply_parts: list[str] = []
    gen_usage: dict = {}

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
            yield sse_event("delta", {"text": chunk})

        final_reply = "".join(full_reply_parts).strip()
        gen_usage = stream_metadata.usage
        trace_total_tokens += gen_usage.get("total_tokens", 0)
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as generation_error:
        entity_label = "礼装" if domain == "ce" else "从者"
        final_reply = f"为你找到了 {total_found} 位{entity_label}。"
        if not full_reply_parts:
            try:
                yield sse_event("delta", {"text": final_reply})
            except Exception:
                pass
        await log_trace_event(
            trace_id,
            "generation_output",
            {"reply": final_reply},
            error=str(generation_error),
        )
    else:
        await log_trace_event(
            trace_id,
            "generation_output",
            {"reply": final_reply, "generation_usage": gen_usage},
        )

    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - stream_start) * 1000, 2),
            "total_found": total_found,
            "result": "success",
            "mode": "confirmation_direct",
            "total_tokens": trace_total_tokens,
        },
    )

    yield sse_event("done", {"model": model_used, "traceId": trace_id})


async def stream_event_generator(
    message: str,
    preset_name: str | None = None,
    *,
    client_ip: str = "unknown",
    confirmation_context: str | None = None,
    confirmation_id: str | None = None,
):
    """SSE 流式事件生成器 — 分阶段推送思考过程和结果。

    从 main.py chat_stream() 内部的 event_generator() 抽取。

    Args:
        confirmation_context: 用户确认选择后回传的上下文，拼接到 message 进行精确路由。
        confirmation_id: 用户选择的选项 ID（collectionNo），用于精确定位实体跳过路由。
    """
    trace_id = uuid.uuid4().hex[:8]
    stream_start = time.monotonic()
    model_used = "unknown"
    trace_total_tokens = 0  # 累计 token 消耗

    # ── 确认直达：用户通过 clarification 选择了具体实体（collectionNo），跳过路由+执行 ──
    if confirmation_id and confirmation_id.isdigit():
        async for event in _handle_confirmation_direct(
            message=message,
            confirmation_id=int(confirmation_id),
            confirmation_context=confirmation_context or "",
            trace_id=trace_id,
            stream_start=stream_start,
            client_ip=client_ip,
        ):
            yield event
        return

    # 用户确认后的第二次请求（自定义输入，无 confirmation_id）：将选择上下文拼接到消息
    if confirmation_context:
        message = f"{message}\n[用户确认：{confirmation_context}]"

    # ── 阶段 1: Skill 路由（或 Preset 展开） ──
    if preset_name:
        # Preset 模式：跳过 LLM 路由，直接展开预设
        yield sse_event("thinking", {"phase": "routing", "message": "正在解析预设..."})

        preset = PRESET_REGISTRY.get(preset_name)
        if preset is None:
            yield sse_event("error", {"phase": "routing", "message": f"未知的预设：{preset_name}"})
            return

        response_skill_name = preset.response_skill
        skill_calls = []
        for skill_name in preset.query_skills:
            merged_params = {**preset.param_template.get(skill_name, {})}
            skill_calls.append({"skill_name": skill_name, "params": merged_params})

        model_used = "preset_mode"

        # B1 策略：用户补充文字走 Stage 2 LLM 路由解析额外 Skills 并合并
        user_text = message.strip()
        if user_text:
            try:
                skill_descriptions = [
                    {"name": s.name, "description": s.description}
                    for s in SKILL_REGISTRY.values()
                    if isinstance(s, QuerySkill)
                ]
                routing_prompt = build_routing_prompt(
                    skill_descriptions,
                    preset_context={
                        "display_name": preset.display_name,
                        "query_skills": preset.query_skills,
                    },
                )
                extra_routing = await chat_completion(
                    system_prompt=routing_prompt,
                    user_message=user_text,
                    temperature=0.1,
                    json_mode=True,
                    response_schema=routing_response_json_schema,
                    response_validator=parse_routing_response,
                )
                extra_routing.pop("_model", None)
                extra_routing.pop("_response_format", None)
                extra_routing.pop("_provider", None)
                extra_routing.pop("_attempts", None)
                extra_skills = extra_routing.get("skill_calls", [])
                # 合并：同名 Skill 补充参数，新 Skill 追加
                existing_map = {s["skill_name"]: s for s in skill_calls}
                for es in extra_skills:
                    es_name = es.get("skill_name")
                    if es_name in existing_map:
                        # 同名 Skill：LLM 解析的参数补充 preset 缺失的字段
                        for k, v in es.get("params", {}).items():
                            if k not in existing_map[es_name]["params"]:
                                existing_map[es_name]["params"][k] = v
                    else:
                        skill_calls.append(es)
                        existing_map[es_name] = es
                extra_resp_skill = extra_routing.get("response_skill")
                if extra_resp_skill and extra_resp_skill != "respond_servant_list":
                    response_skill_name = extra_resp_skill
                # B1 合并日志将通过后续 trace event 记录
            except Exception:
                # 补充解析失败不影响预设查询（静默，trace 中可见）
                pass

        # ── Trace: routing_input (preset) ──
        await log_trace_event(
            trace_id,
            "routing_input",
            {
                "query": message,
                "source": "preset",
                "preset_name": preset_name,
                "skill_count": len(skill_calls),
                "client_ip": client_ip,
            },
        )

        # ── Trace: routing_output (preset) ──
        await log_trace_event(
            trace_id,
            "routing_output",
            {
                "skill_calls": skill_calls,
                "response_skill": response_skill_name,
                "fallback": None,
                "model": model_used,
                "target_pipeline": "A",
            },
        )

        # 推送路由结果（预消化：中文描述替代原始英文 skill_name）
        yield sse_event(
            "thinking",
            {
                "phase": "routed",
                "message": "意图识别完成",
                "detail": "、".join(describe_filters(skill_calls)),
            },
        )

    else:
        # 普通模式：两阶段路由（ADR-024）
        yield sse_event("thinking", {"phase": "routing", "message": "正在分析问题类型..."})

        skill_descriptions = [
            {"name": s.name, "description": s.description} for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)
        ]

        # ── Trace: routing_input ──
        await log_trace_event(
            trace_id,
            "routing_input",
            {
                "query": message,
                "mode": "two_stage",
                "skill_count": len(skill_descriptions),
                "client_ip": client_ip,
                "is_confirmation": confirmation_context is not None,
                "confirmation_context": confirmation_context[:200] if confirmation_context else None,
            },
        )

        # ══════════════════════════════════════════════════════
        # Stage 0: 链路分类器（ADR-024）
        # ══════════════════════════════════════════════════════
        classifier_prompt = build_classifier_prompt()
        sse_classifier_result = None
        _sse_classifier_error = None
        for _sse_cls_attempt in range(2):
            try:
                sse_classifier_result = await chat_completion(
                    system_prompt=classifier_prompt,
                    user_message=message,
                    temperature=0.0,
                    json_mode=True,
                    response_schema=classifier_response_json_schema,
                    response_validator=parse_classifier_response,
                )
                break
            except Exception as cls_err:
                _sse_classifier_error = cls_err
                if _sse_cls_attempt == 0:
                    print(f"⚠️ [{trace_id}] SSE Stage 0 分类第 1 次尝试失败，重试中: {cls_err}")

        # Stage 0 失败 → 降级走 Stage 1 全量路由
        if sse_classifier_result is None:
            print(f"⚠️ [{trace_id}] SSE Stage 0 分类 2 次均失败，降级全量路由: {_sse_classifier_error}")
            sse_classified_pipeline = "A"
            sse_classifier_confidence = 1.0
        else:
            sse_cls_model = sse_classifier_result.pop("_model", "unknown")
            sse_classifier_result.pop("_response_format", None)
            sse_classifier_result.pop("_provider", None)
            sse_classifier_result.pop("_attempts", None)
            sse_cls_usage = sse_classifier_result.pop("_usage", {})
            trace_total_tokens += sse_cls_usage.get("total_tokens", 0)
            sse_classified_pipeline = sse_classifier_result.get("pipeline", "A")
            sse_classifier_confidence = sse_classifier_result.get("confidence", 0.0)

            await log_trace_event(
                trace_id,
                "classifier_output",
                {
                    "pipeline": sse_classified_pipeline,
                    "confidence": sse_classifier_confidence,
                    "model": sse_cls_model,
                    "usage": sse_cls_usage,
                },
            )

        # ── Stage 0 分发：B/C 链路直接处理 ──
        if sse_classified_pipeline in ("B", "C"):
            pipeline_label = "Atlas 知识检索" if sse_classified_pipeline == "B" else "攻略文档检索"
            yield sse_event("thinking", {"phase": "routing", "message": f"识别为{pipeline_label}，正在检索..."})
            if sse_classified_pipeline == "B":
                atlas_response = await _handle_atlas_pipeline(
                    message, trace_id, model_used, stream_start, trace_total_tokens
                )
                try:
                    yield sse_event("delta", {"text": atlas_response.reply})
                except Exception:
                    pass
                yield sse_event("done", {"model": atlas_response.model, "traceId": trace_id})
                return
            else:
                # 链路 C：流式 generation
                guide_result = _prepare_guide_context(message)

                await log_trace_event(
                    trace_id,
                    "guide_search",
                    {"query": message, "result_count": len(guide_result[0]) if guide_result else 0},
                )

                if guide_result is None:
                    no_match_reply = "抱歉，我在攻略库中未找到相关内容。你可以尝试更具体的关键词，如职阶名或关卡名。"
                    yield sse_event("delta", {"text": no_match_reply})
                    await log_trace_event(
                        trace_id,
                        "final",
                        {
                            "total_time_ms": round((time.monotonic() - stream_start) * 1000, 2),
                            "result": "guide_no_match",
                            "mode": "guide_pipeline",
                            "total_tokens": trace_total_tokens,
                        },
                    )
                    yield sse_event("done", {"model": model_used, "traceId": trace_id})
                    return

                chunks, source_labels, source_authors = guide_result
                guide_context = _build_guide_context_text(chunks)
                generation_prompt = _build_guide_generation_prompt(guide_context, message)

                full_reply_parts: list[str] = []
                guide_gen_metadata = StreamMetadata()
                guide_gen_failed = False
                _PROVIDER_FALLBACK_PREFIX = "抱歉，生成服务暂时不可用"
                try:
                    async for chunk in chat_completion_stream(
                        system_prompt=generation_prompt,
                        user_message=message,
                        temperature=0.3,
                        max_tokens=2048,
                        metadata=guide_gen_metadata,
                    ):
                        full_reply_parts.append(chunk)
                        yield sse_event("delta", {"text": chunk})

                    guide_gen_reply = "".join(full_reply_parts).strip()
                    # provider 全链路失败时 yield 兜底文本而非抛异常，需检测
                    if not guide_gen_reply or guide_gen_reply.startswith(_PROVIDER_FALLBACK_PREFIX):
                        raise ValueError(
                            f"Guide generation failed: {'provider fallback' if guide_gen_reply else 'empty response'}"
                        )
                except Exception as guide_gen_error:
                    guide_gen_failed = True
                    fallback_reply = "抱歉，攻略生成服务暂时繁忙，请稍后重试。"
                    # 仅当没有任何内容被推送时才发送兜底消息
                    has_meaningful_output = any(part.strip() for part in full_reply_parts)
                    if not has_meaningful_output:
                        try:
                            yield sse_event("delta", {"text": fallback_reply})
                        except Exception:
                            pass
                    await log_trace_event(
                        trace_id,
                        "generation_output",
                        {"reply": fallback_reply},
                        error=str(guide_gen_error),
                    )
                else:
                    source_suffix = _format_source_suffix(source_labels, source_authors)
                    if source_suffix:
                        full_reply_parts.append(source_suffix)
                        yield sse_event("delta", {"text": source_suffix})

                    await log_trace_event(
                        trace_id,
                        "generation_output",
                        {"reply": guide_gen_reply, "generation_usage": guide_gen_metadata.usage},
                    )

                # generation 实际使用的模型；全链路失败时 metadata.model 为空，回退到分类器模型
                guide_model_used = guide_gen_metadata.model or sse_cls_model
                guide_gen_usage = guide_gen_metadata.usage
                trace_total_tokens += guide_gen_usage.get("total_tokens", 0)

                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": round((time.monotonic() - stream_start) * 1000, 2),
                        "result": "guide_generation_failed" if guide_gen_failed else "guide_pipeline",
                        "mode": "guide_pipeline",
                        "guide_chunks": len(chunks),
                        "sources": list(source_labels),
                        "total_tokens": trace_total_tokens,
                    },
                )
                yield sse_event("done", {"model": guide_model_used, "traceId": trace_id})
                return

        # ── Stage 0 分发：A 链路低置信度 → Agent fallback ──
        if sse_classified_pipeline == "A" and sse_classifier_confidence < 0.6:
            yield sse_event("thinking", {"phase": "agent_fallback", "message": "正在启动智能搜索..."})
            try:
                agent_result = await agent_route(message, TOOL_HANDLERS, trace_id)
                trace_total_tokens += agent_result.total_tokens
                category, clean_reply = classify_agent_reply(agent_result.reply)
                for progress_msg in _build_agent_progress_messages(agent_result.tool_trace):
                    yield sse_event("thinking", {"phase": "agent_tool", "message": progress_msg})
                if agent_result.servants_data and not category:
                    returned = agent_result.servants_data[:MAX_RESULTS]
                    yield sse_event(
                        "servants",
                        {"servants": returned, "count": len(returned), "total": len(agent_result.servants_data)},
                    )
                yield sse_event("delta", {"text": clean_reply})
                await log_trace_event(
                    trace_id,
                    "agent_detail",
                    {
                        "rounds": agent_result.rounds,
                        "agent_tokens": agent_result.total_tokens,
                        "tool_trace": agent_result.tool_trace,
                        "reply": clean_reply,
                    },
                )
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "classifier_low_confidence_agent",
                        "mode": "agent_fallback",
                        "classifier_confidence": sse_classifier_confidence,
                        "total_tokens": trace_total_tokens,
                    },
                )
                yield sse_event("done", {"model": f"agent_{agent_result.rounds}r", "traceId": trace_id})
                return
            except Exception:
                pass  # Agent fallback 失败，继续走 Stage 1

        # ══════════════════════════════════════════════════════
        # Stage 1: Skill 选择 + 参数提取（仅链路 A）
        # ══════════════════════════════════════════════════════
        yield sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})
        routing_prompt = build_routing_prompt(skill_descriptions)

        # 路由失败重试：最多 2 次尝试，全部失败走 Agent 兜底
        routing_result = None
        _sse_routing_last_error = None
        for _sse_routing_attempt in range(2):
            try:
                routing_result = await chat_completion(
                    system_prompt=routing_prompt,
                    user_message=message,
                    temperature=0.1,
                    json_mode=True,
                    response_schema=routing_response_json_schema,
                    response_validator=parse_routing_response,
                )
                break
            except Exception as retry_err:
                _sse_routing_last_error = retry_err
                if _sse_routing_attempt == 0:
                    print(f"⚠️ [{trace_id}] SSE Stage 1 路由第 1 次尝试失败，重试中: {retry_err}")

        if routing_result is None:
            print(f"⚠️ [{trace_id}] SSE 路由 2 次均失败，降级到 Agent: {_sse_routing_last_error}")
            yield sse_event("thinking", {"phase": "agent_fallback", "message": "路由异常，正在启动智能搜索..."})
            try:
                agent_result = await agent_route(message, TOOL_HANDLERS, trace_id)
                trace_total_tokens += agent_result.total_tokens
                category, clean_reply = classify_agent_reply(agent_result.reply)
                for progress_msg in _build_agent_progress_messages(agent_result.tool_trace):
                    yield sse_event("thinking", {"phase": "agent_tool", "message": progress_msg})
                if agent_result.servants_data and not category:
                    returned = agent_result.servants_data[:MAX_RESULTS]
                    yield sse_event(
                        "servants",
                        {"servants": returned, "count": len(returned), "total": len(agent_result.servants_data)},
                    )
                yield sse_event("delta", {"text": clean_reply})
                await log_trace_event(
                    trace_id,
                    "agent_detail",
                    {
                        "rounds": agent_result.rounds,
                        "agent_tokens": agent_result.total_tokens,
                        "tool_trace": agent_result.tool_trace,
                        "reply": clean_reply,
                    },
                )
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "routing_retry_agent_fallback",
                        "mode": "agent_fallback",
                        "total_tokens": trace_total_tokens,
                    },
                )
            except Exception as agent_err:
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "routing_error",
                        "mode": "routing_error",
                        "total_tokens": trace_total_tokens,
                    },
                    error=f"routing: {_sse_routing_last_error}; agent: {agent_err}",
                )
                yield sse_event("error", {"phase": "routing", "message": "路由失败，请稍后重试"})
            yield sse_event("done", {"model": "error", "traceId": trace_id})
            return

        model_used = routing_result.pop("_model", "unknown")
        routing_result.pop("_response_format", None)
        routing_result.pop("_provider", None)
        routing_result.pop("_attempts", None)
        routing_usage = routing_result.pop("_usage", {})
        trace_total_tokens += routing_usage.get("total_tokens", 0)

        skill_calls = routing_result.get("skill_calls", [])
        response_skill_name = routing_result.get("response_skill", "respond_servant_list")

        # ── Trace: routing_output ──
        await log_trace_event(
            trace_id,
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
                    "total_time_ms": (time.monotonic() - stream_start) * 1000,
                    "result": "clarification_requested",
                    "mode": "clarification",
                    "total_tokens": trace_total_tokens,
                },
            )
            yield sse_event(
                "clarification",
                {
                    "question": clarification["question"],
                    "options": clarification["options"],
                    "trace_id": trace_id,
                },
            )
            yield sse_event("done", {"model": model_used, "traceId": trace_id, "needs_confirmation": True})
            return

        # 推送路由结果（预消化：中文描述替代原始英文 skill_name）
        yield sse_event(
            "thinking",
            {
                "phase": "routed",
                "message": "意图识别完成",
                "detail": "、".join(describe_filters(skill_calls)),
            },
        )

        # 检查 fallback
        fallback = routing_result.get("fallback")
        if fallback is not None:
            fb_code = fallback.get("code", "no_match")
            fb_msg = fallback.get("message", "无法理解你的问题，请尝试更具体的描述。")
            # greeting / out_of_scope → 直接模板回复，无需 Agent
            if fb_code in ("greeting", "out_of_scope"):
                template_reply = FALLBACK_TEMPLATES.get(fb_code.upper(), fb_msg)
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": f"fallback_{fb_code}",
                        "mode": f"fallback_{fb_code}",
                        "total_tokens": trace_total_tokens,
                    },
                )
                yield sse_event("delta", {"text": template_reply})
                yield sse_event("done", {"model": model_used, "traceId": trace_id})
                return
            # no_match / ambiguous → Agent 兜底
            yield sse_event("thinking", {"phase": "agent_fallback", "message": "需要更深入分析，启动智能搜索..."})
            try:
                agent_result: AgentResult = await agent_route(message, TOOL_HANDLERS, trace_id)
                trace_total_tokens += agent_result.total_tokens
                category, clean_reply = classify_agent_reply(agent_result.reply)
                for progress_msg in _build_agent_progress_messages(agent_result.tool_trace):
                    yield sse_event("thinking", {"phase": "agent_tool", "message": progress_msg})
                if agent_result.servants_data and not category:
                    returned = agent_result.servants_data[:MAX_RESULTS]
                    yield sse_event(
                        "servants",
                        {"servants": returned, "count": len(returned), "total": len(agent_result.servants_data)},
                    )
                yield sse_event("delta", {"text": clean_reply})
                await log_trace_event(
                    trace_id,
                    "agent_detail",
                    {
                        "rounds": agent_result.rounds,
                        "agent_tokens": agent_result.total_tokens,
                        "tool_trace": agent_result.tool_trace,
                        "agent_elapsed_ms": round(agent_result.elapsed_ms, 2),
                        "reply": clean_reply,
                    },
                )
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "agent_fallback",
                        "mode": "agent_fallback",
                        "total_found": len(agent_result.servants_data),
                        "total_tokens": trace_total_tokens,
                    },
                )
            except Exception as agent_err:
                print(f"⚠️ [{trace_id}] Agent 兜底也失败: {agent_err}")
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "fallback",
                        "mode": "fallback_no_match",
                        "total_tokens": trace_total_tokens,
                    },
                )
                yield sse_event("delta", {"text": fb_msg})
            yield sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        # 空 skill_calls 且无 fallback → Agent 兜底
        if not skill_calls:
            yield sse_event("thinking", {"phase": "agent_fallback", "message": "需要更深入分析，启动智能搜索..."})
            try:
                agent_result = await agent_route(message, TOOL_HANDLERS, trace_id)
                trace_total_tokens += agent_result.total_tokens
                category, clean_reply = classify_agent_reply(agent_result.reply)
                for progress_msg in _build_agent_progress_messages(agent_result.tool_trace):
                    yield sse_event("thinking", {"phase": "agent_tool", "message": progress_msg})
                if agent_result.servants_data and not category:
                    returned = agent_result.servants_data[:MAX_RESULTS]
                    yield sse_event(
                        "servants",
                        {"servants": returned, "count": len(returned), "total": len(agent_result.servants_data)},
                    )
                yield sse_event("delta", {"text": clean_reply})
                await log_trace_event(
                    trace_id,
                    "agent_detail",
                    {
                        "rounds": agent_result.rounds,
                        "agent_tokens": agent_result.total_tokens,
                        "tool_trace": agent_result.tool_trace,
                        "agent_elapsed_ms": round(agent_result.elapsed_ms, 2),
                        "reply": clean_reply,
                    },
                )
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "agent_fallback",
                        "mode": "agent_fallback",
                        "total_found": len(agent_result.servants_data),
                        "total_tokens": trace_total_tokens,
                    },
                )
            except Exception as agent_err:
                print(f"⚠️ [{trace_id}] Agent 兜底也失败: {agent_err}")
                no_match_msg = "无法从你的问题中识别出查询条件，请尝试更具体的描述。"
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "no_match",
                        "mode": "fallback_no_match",
                        "total_tokens": trace_total_tokens,
                    },
                )
                yield sse_event("delta", {"text": no_match_msg})
            yield sse_event("done", {"model": model_used, "traceId": trace_id})
            return

    # ── 阶段 2: Skill 执行 ──
    # 判断是否包含知识类 Skill（用于动态调整 thinking message）
    has_knowledge_skill = (
        any(getattr(SKILL_REGISTRY.get(c.get("skill_name", "")), "domain", "servant") != "servant" for c in skill_calls)
        if skill_calls
        else False
    )
    executing_msg = "正在检索知识库..." if has_knowledge_skill else "正在检索从者数据..."
    yield sse_event("thinking", {"phase": "executing", "message": executing_msg})

    executor = SkillExecutor()
    result = executor.execute(skill_calls, response_skill_name)
    servants = result.servants
    total_found = result.total_found
    returned_servants = servants[:MAX_RESULTS]

    # ── Trace: execution ──
    await log_trace_event(
        trace_id,
        "execution",
        {
            "accepted_skills": result.accepted_skills,
            "rejected_skills": result.rejected_skills,
            "total_found": total_found,
            "execution_time_ms": round(result.execution_time_ms, 2),
            "is_fallback": result.is_fallback,
            "has_clarification": result.clarification is not None,
        },
    )

    # ── 执行层 clarification 检测 ──
    if result.clarification:
        from server.skills.executor import CLARIFICATION_EMPTY_NAME

        # 名称查询空结果：异步 LLM 猜测填充候选
        if result.clarification.get("type") == CLARIFICATION_EMPTY_NAME:
            yield sse_event("thinking", {"phase": "resolving", "message": "正在智能识别..."})
            result = await executor.guess_candidates_async(result)

        # 猜测后仍有 clarification → 推送给前端
        if result.clarification:
            await log_trace_event(
                trace_id,
                "execution_clarification_requested",
                {
                    "type": result.clarification.get("type", ""),
                    "question": result.clarification.get("question", ""),
                    "options": result.clarification.get("options", []),
                    "ambiguous_field": result.clarification.get("ambiguous_field", ""),
                },
            )
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - stream_start) * 1000,
                    "result": "execution_clarification_requested",
                    "mode": "clarification",
                    "total_tokens": trace_total_tokens,
                },
            )
            yield sse_event(
                "clarification",
                {
                    "question": result.clarification["question"],
                    "options": result.clarification.get("options", []),
                    "trace_id": trace_id,
                },
            )
            yield sse_event("done", {"model": model_used, "traceId": trace_id, "needs_confirmation": True})
            return

        # 猜测成功（clarification 被清除），更新结果变量
        servants = result.servants
        total_found = result.total_found
        returned_servants = servants[:MAX_RESULTS]

    # 执行阶段 fallback：先尝试异步昵称识别，再 Agent 兜底
    if result.is_fallback:
        # 异步 resolve_nickname fallback（LLM 昵称识别）
        result = await executor.try_resolve_nickname_async(result, skill_calls)
        if not result.is_fallback:
            # 昵称识别成功，更新变量走正常 RAG 路径
            servants = result.servants
            total_found = result.total_found
            returned_servants = servants[:MAX_RESULTS]
            await log_trace_event(
                trace_id,
                "execution_resolve_nickname",
                {
                    "accepted_skills": result.accepted_skills,
                    "total_found": total_found,
                    "execution_time_ms": round(result.execution_time_ms, 2),
                },
            )

    if result.is_fallback:
        oneshot_ctx = build_oneshot_context(skill_calls)
        fb_reply = result.fallback_message or "未找到匹配的从者。"
        yield sse_event("thinking", {"phase": "agent_fallback", "message": "需要更深入分析，启动智能搜索..."})
        try:
            agent_result = await agent_route(message, TOOL_HANDLERS, trace_id, oneshot_context=oneshot_ctx)
            trace_total_tokens += agent_result.total_tokens
            category, clean_reply = classify_agent_reply(agent_result.reply)
            for progress_msg in _build_agent_progress_messages(agent_result.tool_trace):
                yield sse_event("thinking", {"phase": "agent_tool", "message": progress_msg})
            if agent_result.servants_data and not category:
                returned = agent_result.servants_data[:MAX_RESULTS]
                yield sse_event(
                    "servants",
                    {"servants": returned, "count": len(returned), "total": len(agent_result.servants_data)},
                )
            yield sse_event("delta", {"text": clean_reply})
            await log_trace_event(
                trace_id,
                "agent_detail",
                {
                    "rounds": agent_result.rounds,
                    "agent_tokens": agent_result.total_tokens,
                    "tool_trace": agent_result.tool_trace,
                    "agent_elapsed_ms": round(agent_result.elapsed_ms, 2),
                    "reply": clean_reply,
                },
            )
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - stream_start) * 1000,
                    "result": "agent_fallback",
                    "mode": "agent_fallback",
                    "total_found": len(agent_result.servants_data),
                    "total_tokens": trace_total_tokens,
                },
            )
        except Exception as agent_err:
            print(f"⚠️ [{trace_id}] Agent 兜底也失败: {agent_err}")
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - stream_start) * 1000,
                    "total_found": 0,
                    "result": "execution_fallback",
                    "mode": "execution_fallback",
                    "total_tokens": trace_total_tokens,
                },
            )
            yield sse_event("delta", {"text": fb_reply})
        yield sse_event("done", {"model": model_used, "traceId": trace_id})
        return

    # 卡片先行 — 有从者时推送卡片数据
    if returned_servants:
        yield sse_event(
            "servants",
            {
                "servants": returned_servants,
                "count": len(returned_servants),
                "total": total_found,
            },
        )

    # ── 阶段 3: RAG 生成 ──
    yield sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

    applied_filters = describe_filters(skill_calls)

    # custom_context 路径：知识/配队数据直接作为 context
    if result.custom_context:
        context_data = {
            "知识数据": result.custom_context,
            "已应用的筛选条件": applied_filters,
            "关联从者数量": total_found,
        }
    elif response_skill_name == "respond_ce_list":
        # CE domain：使用礼装专用 context 构建
        context_data, _ = build_ce_context(servants, skill_calls=skill_calls)
        context_data["已应用的筛选条件"] = applied_filters
        context_data["筛选条件"] = applied_filters
    else:
        detail_mode = response_skill_name == "respond_servant_detail"
        context_data, _ = build_context(servants, detail_mode=detail_mode, skill_calls=skill_calls)
        context_data["已应用的筛选条件"] = applied_filters
        context_data["筛选条件"] = applied_filters

    context_json = json.dumps(context_data, ensure_ascii=False)

    # ── Trace: context_build ──
    await log_trace_event(
        trace_id,
        "context_build",
        {
            "applied_filters": applied_filters,
            "context_data": context_data,
        },
    )

    # 使用 Response Skill 的 prompt（如果可用）
    if result.response_skill is not None:
        gen_prompt = result.response_skill.build_prompt(message, context_json)
    else:
        gen_prompt = get_generation_prompt(message, context_json)

    # ── Trace: generation_input ──
    await log_trace_event(
        trace_id,
        "generation_input",
        {
            "generation_prompt": gen_prompt,
        },
    )

    final_result = "success"
    gen_usage: dict = {}
    full_reply_parts: list[str] = []

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
            yield sse_event("delta", {"text": chunk})

        final_reply = "".join(full_reply_parts).strip()
        gen_usage = stream_metadata.usage
        trace_total_tokens += gen_usage.get("total_tokens", 0)
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as e:
        final_reply = (
            f"为你找到了 {total_found} 个礼装。"
            if response_skill_name == "respond_ce_list"
            else (
                f"为你找到了 {total_found} 位从者。" if not result.custom_context else "暂时无法生成回复，请稍后重试。"
            )
        )
        final_result = "generation_error"
        # 如果流式过程中已推送了部分内容，不再重复推送 fallback
        if full_reply_parts:
            final_reply = "".join(full_reply_parts).strip()
            final_result = "success"
        else:
            try:
                yield sse_event("delta", {"text": final_reply})
            except Exception:
                final_result = "client_disconnected"
        await log_trace_event(
            trace_id,
            "generation_output",
            {
                "reply": final_reply,
            },
            error=str(e),
        )
    else:
        # ── Trace: generation_output (success) ──
        await log_trace_event(
            trace_id,
            "generation_output",
            {
                "reply": final_reply,
                "generation_usage": gen_usage,
            },
        )

    # ── Trace: final ──
    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - stream_start) * 1000, 2),
            "total_found": total_found,
            "result": final_result,
            "mode": "oneshot",
            "total_tokens": trace_total_tokens,
        },
    )

    # ── 完成 ──
    done_data: dict = {"model": model_used, "traceId": trace_id}
    if result.rejected_skills:
        done_data["rejected_skills"] = result.rejected_skills
    yield sse_event("done", done_data)


# ============================================================
# 链路 B/C 处理函数
# ============================================================


async def _extract_atlas_query(user_message: str, trace_id: str) -> dict | None:
    """独立 LLM 调用：从用户查询中提取 Atlas 结构化参数。

    Stage 0 只做链路分类不提取参数，链路 B 需要单独调用此函数提取
    name/entry_type/year_month 等结构化字段。

    Returns:
        提取到的参数字典，失败时返回 None（由调用方降级处理）。
    """
    extraction_prompt = """你是 FGO 知识查询参数提取器。从用户问题中提取结构化查询参数。

## 输出字段
- `name`: 具体的活动名/卡池名/关卡名/素材名/从者名（字符串），不要填入整个问句
- `entry_type`: 条目类型（event=活动/war=主线关卡/gacha=卡池/item=素材）
- `tag`: 标签（如 event_type:campaign，可选）
- `year_month`: 时间（YYYY-MM 格式，可选）

## 输出格式
严格按 JSON 格式输出，只包含能从问题中提取到的字段，无法确定的字段不要填：
```json
{"name": "梅林", "entry_type": "gacha"}
```

## 示例
用户："梅林什么时候复刻" → {"name": "梅林", "entry_type": "gacha"}
用户："最近有什么活动" → {"entry_type": "event"}
用户："特异点F是什么" → {"name": "特异点F", "entry_type": "war"}
用户："龙之牙在哪里掉" → {"name": "龙之牙", "entry_type": "item"}
用户："去年周年庆" → {"entry_type": "event", "year_month": "2025-07"}
"""
    try:
        result = await chat_completion(
            system_prompt=extraction_prompt,
            user_message=user_message,
            temperature=0.0,
            json_mode=True,
        )
        import json as _json

        from server.llm import extract_json_object

        text = result.get("text", "")
        extracted = _json.loads(extract_json_object(text))

        await log_trace_event(
            trace_id,
            "atlas_query_extraction",
            {"query": user_message, "extracted": extracted},
        )
        return extracted
    except Exception as extraction_err:
        print(f"⚠️ [{trace_id}] Atlas 参数提取失败，降级原始查询: {extraction_err}")
        return None


async def _handle_atlas_pipeline(
    user_message: str,
    trace_id: str,
    model_used: str,
    request_start: float,
    trace_total_tokens: int,
    atlas_query: dict | None = None,
) -> ChatResponse:
    """链路 B：Atlas 知识问答。检索 Atlas CN 索引 → LLM 生成回复 → 事实校验。"""
    from server.atlas_index import AtlasQueryParams, get_atlas_index

    atlas = get_atlas_index()

    # 优先使用路由 LLM 提取的结构化参数
    query_source = "structured"
    if atlas_query:
        params = AtlasQueryParams(**atlas_query)
    elif atlas_query is None:
        # Stage 0 只做分类不提取参数 → 独立 LLM 调用提取 atlas_query
        query_source = "llm_extraction"
        extracted_query = await _extract_atlas_query(user_message, trace_id)
        if extracted_query:
            params = AtlasQueryParams(**extracted_query)
            atlas_query = extracted_query
        else:
            query_source = "raw_fallback"
            params = AtlasQueryParams(name=user_message)

    results = atlas.search(params)

    await log_trace_event(
        trace_id,
        "atlas_search",
        {"query": user_message, "result_count": len(results), "query_source": query_source, "atlas_query": atlas_query},
    )

    # 结构化搜索无结果时，降级到原始消息匹配
    if not results and query_source == "structured":
        query_source = "raw_fallback"
        params = AtlasQueryParams(name=user_message)
        results = atlas.search(params)
        await log_trace_event(
            trace_id,
            "atlas_search_fallback",
            {"query": user_message, "result_count": len(results), "query_source": query_source},
        )

    if not results:
        reply = "抱歉，我在活动/卡池/主线数据库中未找到相关信息。你可以尝试更具体的关键词，如活动名称或时间段。"
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": round((time.monotonic() - request_start) * 1000, 2),
                "result": "atlas_no_match",
                "mode": "atlas_pipeline",
                "total_tokens": trace_total_tokens,
            },
        )
        return ChatResponse(
            reply=reply, servants=[], count=0, query={"mode": "atlas_pipeline"}, model=model_used, traceId=trace_id
        )

    # 构建上下文摘要供 LLM 参考
    context_lines = []
    for item in results[:10]:
        summary_parts = [f"{k}: {v}" for k, v in item.items() if k != "entry_key"]
        context_lines.append("- " + ", ".join(summary_parts))
    atlas_context = "\n".join(context_lines)

    generation_prompt = (
        "你是 FGO 知识助手。根据以下 Atlas 数据库检索结果回答用户问题。\n"
        "要求：\n"
        "1. 仅基于提供的数据回答，不要编造信息\n"
        "2. 使用中文自然语言，面向玩家\n"
        "3. 如果数据不足以完整回答，明确告知哪些信息缺失\n\n"
        f"检索结果：\n{atlas_context}\n\n"
        f"用户问题：{user_message}"
    )

    gen_result = await chat_completion(
        system_prompt="You are a helpful AI assistant.",
        user_message=generation_prompt,
        temperature=0.3,
        json_mode=False,
    )
    reply = gen_result.get("text", "").strip()
    gen_usage = gen_result.get("_usage", {})
    trace_total_tokens += gen_usage.get("total_tokens", 0)
    model_used = gen_result.get("_model", model_used)

    # 轻量级事实校验
    verified = _verify_atlas_facts(reply, atlas)
    await log_trace_event(
        trace_id,
        "fact_verify",
        {"verified": verified},
    )

    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - request_start) * 1000, 2),
            "result": "atlas_pipeline",
            "mode": "atlas_pipeline",
            "atlas_results": len(results),
            "total_tokens": trace_total_tokens,
        },
    )

    return ChatResponse(
        reply=reply, servants=[], count=0, query={"mode": "atlas_pipeline"}, model=model_used, traceId=trace_id
    )


def _extract_guide_tags(query: str) -> list[str]:
    """从用户查询中提取攻略相关的 tag 关键词，用于缩小 BM25 检索范围。

    仅提取职阶相关 tag，避免全量检索时拉入不相关职阶的攻略文档。
    """
    # 职阶关键词 → 对应 tag（优先匹配中文简称/俗称）
    _CLASS_TAG_MAP: dict[str, str] = {
        "剑": "saber",
        "剑阶": "saber",
        "剑冠": "saber",
        "弓": "archer",
        "弓阶": "archer",
        "弓冠": "archer",
        "枪": "lancer",
        "枪阶": "lancer",
        "枪冠": "lancer",
        "骑": "rider",
        "骑阶": "rider",
        "骑冠": "rider",
        "术": "caster",
        "术阶": "caster",
        "术冠": "caster",
        "杀": "assassin",
        "杀阶": "assassin",
        "杀冠": "assassin",
        "狂": "berserker",
        "狂阶": "berserker",
        "狂冠": "berserker",
        "ex": "extra",
        "EX": "extra",
        "ex冠": "extra",
        "EX冠": "extra",
    }
    tags: list[str] = []
    for keyword, tag in _CLASS_TAG_MAP.items():
        if keyword in query and tag not in tags:
            tags.append(tag)
    return tags


def _prepare_guide_context(user_message: str) -> tuple[list, set[str], dict[str, str]] | None:
    """BM25 检索攻略文档并构建上下文。

    Returns:
        (chunks, source_labels, source_authors) 或 None（无结果时）
    """
    from server.guide_retriever import GuideRetriever

    retriever = GuideRetriever()
    guide_tags = _extract_guide_tags(user_message)
    chunks = retriever.search(user_message, tags=guide_tags or None, top_k=3)

    if not chunks:
        return None

    source_labels: set[str] = set()
    source_authors: dict[str, str] = {}
    for chunk in chunks:
        title = chunk.metadata.get("title", "攻略")
        if title and title != "攻略":
            source_labels.add(title)
            author = chunk.metadata.get("author", "")
            if author:
                source_authors[title] = author

    return chunks, source_labels, source_authors


def _build_guide_context_text(chunks: list) -> str:
    """将 BM25 检索结果拼接为 LLM 上下文字符串。"""
    context_lines = []
    for chunk in chunks:
        title = chunk.metadata.get("title", "攻略")
        section = chunk.metadata.get("section", "")
        header = f"【{title}】" + (f" {section}" if section else "")
        context_lines.append(f"{header}\n{chunk.content}")
    return "\n\n---\n\n".join(context_lines)


def _build_guide_generation_prompt(guide_context: str, user_message: str) -> str:
    """构建链路 C generation 阶段的 system prompt。"""
    return (
        "你是 FGO 攻略助手。根据以下攻略文档内容回答用户问题。\n"
        "要求：\n"
        "1. **严格限定知识范围**：仅基于提供的攻略内容回答，严禁使用自身训练知识补充或猜测。"
        "如果攻略中未涉及用户问的内容，必须明确说「攻略中暂未收录这部分内容」，不要尝试自行回答\n"
        "2. 使用中文自然语言，面向玩家，语气亲切专业\n"
        "3. 如果攻略内容不足以完整回答问题，明确告知哪些部分缺少资料，不要用模糊措辞搪塞\n"
        "4. **[绝对禁止]** 不要在回复中出现任何文件名、文件路径、内部标题格式（如 [xxx] > yyy）、"
        "markdown 标记、技术术语或系统实现细节。回复必须完全是面向玩家的自然语言\n"
        "5. 不要自行添加来源标注，系统会自动处理\n\n"
        f"攻略内容：\n{guide_context}\n\n"
        f"用户问题：{user_message}"
    )


def _format_source_suffix(source_labels: set[str], source_authors: dict[str, str]) -> str:
    """格式化来源标注后缀。"""
    if not source_labels:
        return ""
    labels_with_author = []
    for title in sorted(source_labels):
        author = source_authors.get(title)
        if author:
            labels_with_author.append(f"{title}（作者：**{author}**）")
        else:
            labels_with_author.append(title)
    return "\n\n📖 参考：" + ", ".join(labels_with_author)


async def _handle_guide_pipeline(
    user_message: str,
    trace_id: str,
    model_used: str,
    request_start: float,
    trace_total_tokens: int,
) -> ChatResponse:
    """链路 C：攻略知识问答。BM25 检索攻略文档 → LLM 生成回复 → 来源标注。"""
    result = _prepare_guide_context(user_message)

    await log_trace_event(
        trace_id,
        "guide_search",
        {"query": user_message, "result_count": len(result[0]) if result else 0},
    )

    if result is None:
        reply = "抱歉，我在攻略库中未找到相关内容。你可以尝试更具体的关键词，如职阶名或关卡名。"
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": round((time.monotonic() - request_start) * 1000, 2),
                "result": "guide_no_match",
                "mode": "guide_pipeline",
                "total_tokens": trace_total_tokens,
            },
        )
        return ChatResponse(
            reply=reply, servants=[], count=0, query={"mode": "guide_pipeline"}, model=model_used, traceId=trace_id
        )

    chunks, source_labels, source_authors = result
    guide_context = _build_guide_context_text(chunks)
    generation_prompt = _build_guide_generation_prompt(guide_context, user_message)

    gen_result = await chat_completion(
        system_prompt=generation_prompt,
        user_message=user_message,
        temperature=0.3,
        json_mode=False,
    )
    reply = gen_result.get("text", "").strip()
    gen_usage = gen_result.get("_usage", {})
    trace_total_tokens += gen_usage.get("total_tokens", 0)
    model_used = gen_result.get("_model", model_used)

    reply += _format_source_suffix(source_labels, source_authors)

    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - request_start) * 1000, 2),
            "result": "guide_pipeline",
            "mode": "guide_pipeline",
            "guide_chunks": len(chunks),
            "sources": list(source_labels),
            "total_tokens": trace_total_tokens,
        },
    )

    return ChatResponse(
        reply=reply, servants=[], count=0, query={"mode": "guide_pipeline"}, model=model_used, traceId=trace_id
    )


def _verify_atlas_facts(reply: str, atlas) -> bool:
    """轻量级事实验证：检查回复中提到的实体是否存在于 Atlas 索引中。

    提取回复中的引号内容和专有名词，验证其在索引中存在。
    返回 True 表示验证通过或未检测到可验证实体。
    """
    import re

    # 提取中文引号内的内容作为待验证实体
    quoted_entities = re.findall(r"[\u201c\u201d](.*?)[\u201c\u201d]", reply)
    if not quoted_entities:
        return True

    verified_count = 0
    total_count = 0
    for entity in quoted_entities:
        if len(entity) < 2:
            continue
        total_count += 1
        if atlas.verify_fact("name", entity):
            verified_count += 1

    if total_count == 0:
        return True

    # 超过 70% 的实体可验证即通过
    return verified_count / total_count >= 0.7
