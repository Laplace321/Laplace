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
from server.edges import after_classify, after_execute, after_route
from server.fallback import (
    FALLBACK_TEMPLATES,
    build_oneshot_context,
    classify_agent_reply,
    sse_event,
)
from server.graph import END, PipelineState, StateGraph
from server.llm import StreamMetadata, chat_completion, chat_completion_stream
from server.logger import log_trace_event
from server.nodes.agent import agent_fallback_node
from server.nodes.atlas import atlas_node
from server.nodes.clarify import clarify_node
from server.nodes.classify import classify_node
from server.nodes.execute import execute_node
from server.nodes.fallback import template_fallback_node
from server.nodes.generate import generate_node
from server.nodes.guide import (
    _build_guide_context_text,
    _build_guide_generation_prompt,
    _format_source_suffix,
    _prepare_guide_context,
    guide_node,
)
from server.nodes.route import route_node
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

# ────────────────────────────────────────────────────────────
# Pipeline B/C 图引擎（Task 1 — ADR-028）
# B 与 C 各为单节点图，未来 Task 2/3 会扩展到完整 A 图
# ────────────────────────────────────────────────────────────


def _build_pipeline_b_graph() -> StateGraph:
    """构建 Pipeline B 图：atlas_node → END。"""
    g = StateGraph()
    g.add_node("atlas", atlas_node)
    g.set_entry("atlas")
    g.add_edge("atlas", END)
    return g


def _build_pipeline_c_graph() -> StateGraph:
    """构建 Pipeline C 图：guide_node → END（非流式版）。"""
    g = StateGraph()
    g.add_node("guide", guide_node)
    g.set_entry("guide")
    g.add_edge("guide", END)
    return g


# 模块级缓存图实例（注册过程是确定性的，复用避免每次请求重建）
_PIPELINE_B_GRAPH: StateGraph | None = None
_PIPELINE_C_GRAPH: StateGraph | None = None
_PIPELINE_A_GRAPH: StateGraph | None = None
_PIPELINE_DIRECT_GRAPH: StateGraph | None = None


def _get_pipeline_b_graph() -> StateGraph:
    global _PIPELINE_B_GRAPH
    if _PIPELINE_B_GRAPH is None:
        _PIPELINE_B_GRAPH = _build_pipeline_b_graph()
    return _PIPELINE_B_GRAPH


def _get_pipeline_c_graph() -> StateGraph:
    global _PIPELINE_C_GRAPH
    if _PIPELINE_C_GRAPH is None:
        _PIPELINE_C_GRAPH = _build_pipeline_c_graph()
    return _PIPELINE_C_GRAPH


def _build_pipeline_a_graph() -> StateGraph:
    """构建完整 Pipeline A 图（Task 3 — ADR-028）。

    主路径 + 降级路径全部在图内::

        classify ─┬→ atlas    → END
                  ├→ guide    → END
                  ├→ route    ─┬→ execute ─┬→ generate         → END
                  │            │           ├→ clarify          → END
                  │            │           └→ agent_fallback   → END
                  │            ├→ clarify          → END
                  │            ├→ template_fallback→ END
                  │            └→ agent_fallback   → END
                  └→ agent_fallback   → END   (低置信度)

    所有降级 reason 经 ``edges._dispatch_bail_out`` 分发到 agent / clarify /
    template_fallback 三个节点，节点把结果写入 state 后连 END，
    ``handle_skill_mode`` 拿到 state 直接拼装 ChatResponse。
    """
    g = StateGraph()
    g.add_node("classify", classify_node)
    g.add_node("atlas", atlas_node)
    g.add_node("guide", guide_node)
    g.add_node("route", route_node)
    g.add_node("execute", execute_node)
    g.add_node("generate", generate_node)
    g.add_node("agent_fallback", agent_fallback_node)
    g.add_node("clarify", clarify_node)
    g.add_node("template_fallback", template_fallback_node)
    g.set_entry("classify")
    g.add_conditional_edge("classify", after_classify)
    g.add_edge("atlas", END)
    g.add_edge("guide", END)
    g.add_conditional_edge("route", after_route)
    g.add_conditional_edge("execute", after_execute)
    g.add_edge("generate", END)
    g.add_edge("agent_fallback", END)
    g.add_edge("clarify", END)
    g.add_edge("template_fallback", END)
    return g


def _build_pipeline_direct_graph() -> StateGraph:
    """构建 skill_calls 直传短图（preset / 前端直传）。

    拓扑::

        execute ─┬→ generate         → END
                 ├→ clarify          → END
                 └→ agent_fallback   → END

    跳过 classify/route，直接从 execute 入图；执行层降级（clarification /
    fallback）由对应节点处理后连 END。
    """
    g = StateGraph()
    g.add_node("execute", execute_node)
    g.add_node("generate", generate_node)
    g.add_node("agent_fallback", agent_fallback_node)
    g.add_node("clarify", clarify_node)
    g.set_entry("execute")
    g.add_conditional_edge("execute", after_execute)
    g.add_edge("generate", END)
    g.add_edge("agent_fallback", END)
    g.add_edge("clarify", END)
    return g


def _get_pipeline_a_graph() -> StateGraph:
    global _PIPELINE_A_GRAPH
    if _PIPELINE_A_GRAPH is None:
        _PIPELINE_A_GRAPH = _build_pipeline_a_graph()
    return _PIPELINE_A_GRAPH


def _get_pipeline_direct_graph() -> StateGraph:
    global _PIPELINE_DIRECT_GRAPH
    if _PIPELINE_DIRECT_GRAPH is None:
        _PIPELINE_DIRECT_GRAPH = _build_pipeline_direct_graph()
    return _PIPELINE_DIRECT_GRAPH


def _state_to_chat_response(state: PipelineState, trace_id: str) -> ChatResponse:
    """把 PipelineState 拼装为外部返回的 ChatResponse。"""
    return ChatResponse(
        reply=state.reply,
        servants=state.servants,
        count=state.count,
        query=state.query,
        model=state.model_used,
        traceId=trace_id,
    )


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
    """Skill 模式核心入口（Task 3 起降级路径全部节点化 — ADR-028）。

    主路径（Stage 0 分类 + Stage 1 路由 + Skill 执行 + RAG 生成）由
    ``_get_pipeline_a_graph()`` 完整组装；preset / 前端直传跳过路由走
    ``_get_pipeline_direct_graph()`` 短图（execute → generate）。

    所有降级路径（routing_failed / clarification / fallback / agent fallback /
    execution fallback）已在图内由 ``agent_fallback_node`` / ``clarify_node`` /
    ``template_fallback_node`` 三个节点统一处理，本函数无需再判 bail_out，
    直接把 state 拼装为 ChatResponse。

    Args:
        confirmation_context: 用户确认选择后回传的上下文，拼接到 user_message 进行精确路由。
    """
    if confirmation_context:
        user_message = f"{user_message}\n[用户确认：{confirmation_context}]"
    request_start = time.monotonic()

    # ── 直传 skill_calls 路径（preset / 前端直传）──
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
                "model": "skill_mode",
                "target_pipeline": target_pipeline,
            },
        )

        state = PipelineState(
            user_message=user_message,
            trace_id=trace_id,
            request_start=request_start,
            client_ip=client_ip,
            model_used="skill_mode",
            skill_calls=list(skill_calls),
            response_skill_name=response_skill_name,
            target_pipeline=target_pipeline,
        )
        try:
            state = await _get_pipeline_direct_graph().run(state)
        except Exception as e:
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - request_start) * 1000,
                    "result": "routing_error",
                    "mode": "routing_error",
                    "total_tokens": state.trace_total_tokens,
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

        # 降级路径节点已写入 state.reply / servants / query / model_used，无需再判 bail_out
        return _state_to_chat_response(state, trace_id)

    # ── 完整 Pipeline A 图（Stage 0 → Stage 1 → execute → generate）──
    await log_trace_event(
        trace_id,
        "routing_input",
        {
            "query": user_message,
            "mode": "two_stage",
            "skill_count": sum(1 for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)),
            "client_ip": client_ip,
            "is_confirmation": confirmation_context is not None,
            "confirmation_context": confirmation_context[:200] if confirmation_context else None,
        },
    )

    state = PipelineState(
        user_message=user_message,
        trace_id=trace_id,
        request_start=request_start,
        client_ip=client_ip,
        model_used="skill_mode",
        response_skill_name=response_skill_name,
    )
    state.extras["is_confirmation"] = confirmation_context is not None
    state.extras["confirmation_context_preview"] = confirmation_context[:200] if confirmation_context else None

    try:
        state = await _get_pipeline_a_graph().run(state)
    except Exception as e:
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": "routing_error",
                "mode": "routing_error",
                "total_tokens": state.trace_total_tokens,
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

    if not state.extras.get("bail_out"):
        return _state_to_chat_response(state, trace_id)
    # 降级路径节点已写入 state.reply / servants / query / model_used，无需再判 bail_out
    return _state_to_chat_response(state, trace_id)


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
        detail_mode = response_skill_name in (
            "respond_servant_detail",
            "respond_support_analysis",
            "respond_servant_compare",
        )
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


async def _handle_atlas_pipeline(
    user_message: str,
    trace_id: str,
    model_used: str,
    request_start: float,
    trace_total_tokens: int,
    atlas_query: dict | None = None,
) -> ChatResponse:
    """链路 B：Atlas 知识问答。Task 1 起改为图引擎执行。

    本函数现在是 thin wrapper：构建 PipelineState → graph.run(atlas_node) → 拼装 ChatResponse。
    业务逻辑实现在 ``server.nodes.atlas.atlas_node``。
    """
    state = PipelineState(
        user_message=user_message,
        trace_id=trace_id,
        request_start=request_start,
        model_used=model_used,
        trace_total_tokens=trace_total_tokens,
        atlas_query=atlas_query,
    )
    state = await _get_pipeline_b_graph().run(state)
    return _state_to_chat_response(state, trace_id)


async def _handle_guide_pipeline(
    user_message: str,
    trace_id: str,
    model_used: str,
    request_start: float,
    trace_total_tokens: int,
) -> ChatResponse:
    """链路 C：攻略知识问答。Task 1 起改为图引擎执行（非流式）。

    SSE 流式版本仍由 ``stream_event_generator`` 内联实现（直接调用
    ``_prepare_guide_context`` / ``_build_guide_generation_prompt``），Task 5 统一改造。
    """
    state = PipelineState(
        user_message=user_message,
        trace_id=trace_id,
        request_start=request_start,
        model_used=model_used,
        trace_total_tokens=trace_total_tokens,
    )
    state = await _get_pipeline_c_graph().run(state)
    return _state_to_chat_response(state, trace_id)
