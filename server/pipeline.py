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
from server.llm import chat_completion, chat_completion_stream
from server.logger import log_trace_event
from server.prompts import build_routing_prompt, get_generation_prompt
from server.schemas import parse_routing_response, routing_response_json_schema
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.skills.executor import SkillExecutor
from server.skills.presets import PRESET_REGISTRY
from server.translation import describe_filters


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
) -> ChatResponse:
    """Skill 模式的核心处理逻辑。

    接收已确定的 skill_calls（来自 LLM 路由或前端直传），
    执行 SkillExecutor 并生成 RAG 回复。
    """
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

    # 如果没有传入 skill_calls，通过 LLM 路由获取
    if skill_calls is None:
        skill_descriptions = [
            {"name": s.name, "description": s.description} for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)
        ]
        routing_prompt = build_routing_prompt(skill_descriptions)

        # ── Trace: routing_input ──
        await log_trace_event(
            trace_id,
            "routing_input",
            {
                "query": user_message,
                "mode": "oneshot_llm",
                "routing_prompt_length": len(routing_prompt),
                "skill_count": len(skill_descriptions),
                "client_ip": client_ip,
            },
        )

        try:
            routing_result = await chat_completion(
                system_prompt=routing_prompt,
                user_message=user_message,
                temperature=0.1,
                json_mode=True,
                response_schema=routing_response_json_schema,
                response_validator=parse_routing_response,
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

            # ── 链路 B/C 分发 ──
            target_pipeline = routing_result.get("target_pipeline")
            if target_pipeline == "B":
                return await _handle_atlas_pipeline(
                    user_message,
                    trace_id,
                    model_used,
                    request_start,
                    trace_total_tokens,
                    routing_result.get("atlas_query"),
                )
            if target_pipeline == "C":
                return await _handle_guide_pipeline(
                    user_message, trace_id, model_used, request_start, trace_total_tokens
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
        },
    )

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

    return ChatResponse(
        reply=final_reply,
        servants=returned_servants,
        count=total_found,
        query={"mode": "skill", "skill_calls": skill_calls},
        model=model_used,
        traceId=trace_id,
    )


async def stream_event_generator(message: str, preset_name: str | None = None, *, client_ip: str = "unknown"):
    """SSE 流式事件生成器 — 分阶段推送思考过程和结果。

    从 main.py chat_stream() 内部的 event_generator() 抽取。
    """
    trace_id = uuid.uuid4().hex[:8]
    stream_start = time.monotonic()
    model_used = "unknown"
    trace_total_tokens = 0  # 累计 token 消耗

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
        # 普通模式：Stage 1 LLM 路由
        yield sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})

        skill_descriptions = [
            {"name": s.name, "description": s.description} for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)
        ]
        routing_prompt = build_routing_prompt(skill_descriptions)

        # ── Trace: routing_input ──
        await log_trace_event(
            trace_id,
            "routing_input",
            {
                "query": message,
                "mode": "oneshot_llm",
                "routing_prompt_length": len(routing_prompt),
                "skill_count": len(skill_descriptions),
                "client_ip": client_ip,
            },
        )

        try:
            routing_result = await chat_completion(
                system_prompt=routing_prompt,
                user_message=message,
                temperature=0.1,
                json_mode=True,
                response_schema=routing_response_json_schema,
                response_validator=parse_routing_response,
            )
        except Exception as e:
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - stream_start) * 1000,
                    "result": "routing_error",
                    "mode": "routing_error",
                    "total_tokens": trace_total_tokens,
                },
                error=str(e),
            )
            yield sse_event("error", {"phase": "routing", "message": "路由失败，请稍后重试"})
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

        # ── 链路 B/C 分发（SSE 流式） ──
        target_pipeline = routing_result.get("target_pipeline")
        if target_pipeline in ("B", "C"):
            pipeline_label = "Atlas 知识检索" if target_pipeline == "B" else "攻略文档检索"
            yield sse_event("thinking", {"phase": "routing", "message": f"识别为{pipeline_label}，正在检索..."})
            if target_pipeline == "B":
                atlas_response = await _handle_atlas_pipeline(
                    message,
                    trace_id,
                    model_used,
                    stream_start,
                    trace_total_tokens,
                    routing_result.get("atlas_query"),
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
                async for chunk in chat_completion_stream(
                    system_prompt=generation_prompt,
                    user_message=message,
                    temperature=0.3,
                    max_tokens=2048,
                ):
                    full_reply_parts.append(chunk)
                    yield sse_event("delta", {"text": chunk})

                # 流式结束后追加来源标注
                source_suffix = _format_source_suffix(source_labels, source_authors)
                if source_suffix:
                    full_reply_parts.append(source_suffix)
                    yield sse_event("delta", {"text": source_suffix})

                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": round((time.monotonic() - stream_start) * 1000, 2),
                        "result": "guide_pipeline",
                        "mode": "guide_pipeline",
                        "guide_chunks": len(chunks),
                        "sources": list(source_labels),
                        "total_tokens": "streaming",
                    },
                )
                yield sse_event("done", {"model": model_used, "traceId": trace_id})
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
        },
    )

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
    try:
        generation_response = await chat_completion(
            system_prompt=(
                "You are a helpful AI assistant. You MUST strictly follow "
                "the provided data and NEVER use your internal knowledge about FGO."
            ),
            user_message=gen_prompt,
            temperature=0.1,
            json_mode=False,
        )
        final_reply = generation_response.get("text", "").strip()
        gen_usage = generation_response.get("_usage", {})
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

    # 推送生成的文本（客户端可能已断连，捕获异常确保 final trace 写入）
    try:
        yield sse_event("delta", {"text": final_reply})
    except Exception:
        final_result = "client_disconnected"

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
    yield sse_event("done", {"model": model_used, "traceId": trace_id})


# ============================================================
# 链路 B/C 处理函数
# ============================================================


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


def _prepare_guide_context(user_message: str) -> tuple[list, set[str], dict[str, str]] | None:
    """BM25 检索攻略文档并构建上下文。

    Returns:
        (chunks, source_labels, source_authors) 或 None（无结果时）
    """
    from server.guide_retriever import GuideRetriever

    retriever = GuideRetriever()
    chunks = retriever.search(user_message, top_k=3)

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
        "1. 仅基于提供的攻略内容回答，不要编造信息\n"
        "2. 使用中文自然语言，面向玩家，语气亲切专业\n"
        "3. 如果攻略内容不足以完整回答问题，明确告知哪些部分缺少资料\n"
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
