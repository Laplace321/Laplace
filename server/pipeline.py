"""请求处理管线 — OneShot 路由 + SkillExecutor + RAG 生成（ADR-028 终态）。

本模块对外暴露的核心入口：

- ``handle_skill_mode`` — JSON API 的 Skill 模式处理（路由/执行/生成）
- ``stream_chat_events`` — SSE 流式端点的事件生成器（Task 5 起统一为图引擎驱动）
- ``resume_skill_mode`` — pending checkpoint 恢复执行（Task 4 Batch B）
- ``_handle_atlas_pipeline`` / ``_handle_guide_pipeline`` — 链路 B/C JSON 模式 wrapper（保留供测试引用）

Task 5 起 SSE 流式路径完全由 ``StateGraph.run_stream`` 推进，节点直接 yield 事件，
入口生成器只做三套图的分发（confirmation_id 直达 / preset 直传 / 普通两阶段）。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

# ChatResponse 在 main.py 中定义，这里用 Pydantic BaseModel 重新定义一个兼容类型
# 避免循环导入（pipeline 被 main 导入，main 定义 ChatResponse）
from pydantic import BaseModel

from server.edges import after_classify, after_execute, after_merge_filters, after_route
from server.fallback import sse_event
from server.graph import END, PipelineState, StateGraph
from server.graph.checkpointer import SqliteCheckpointer
from server.graph.session import SessionStore
from server.llm import chat_completion
from server.logger import bind_trace_id, log_trace_event
from server.nodes.agent import agent_fallback_node, agent_fallback_stream_node
from server.nodes.atlas import atlas_node, atlas_stream_node
from server.nodes.clarify import clarify_node
from server.nodes.classify import classify_node
from server.nodes.execute import execute_node
from server.nodes.fallback import template_fallback_node
from server.nodes.generate import generate_node, generate_stream_node
from server.nodes.guide import guide_node, guide_stream_node
from server.nodes.merge_filters import merge_filters_node
from server.nodes.route import route_node
from server.prompts import build_routing_prompt
from server.schemas import parse_routing_response, routing_response_json_schema
from server.skills.base import SKILL_REGISTRY, QuerySkill, ResponseSkill
from server.skills.executor import ExecutionResult
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
_PIPELINE_A_STREAM_GRAPH: StateGraph | None = None
_PIPELINE_DIRECT_STREAM_GRAPH: StateGraph | None = None
_PIPELINE_CONFIRMATION_STREAM_GRAPH: StateGraph | None = None


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
    """构建完整 Pipeline A 图（Task 3 — ADR-028；Task 4 Batch B 加入 merge_filters）。

    主路径 + 降级路径全部在图内::

        classify ─┬→ atlas         → END
                  ├→ guide         → END
                  ├→ merge_filters ─┬→ execute (MINOR/CORRECTION 合并成功)
                  │                 └→ route   (合并失败降级)
                  ├→ route          ─┬→ execute ─┬→ generate         → END
                  │                  │           ├→ clarify          → END
                  │                  │           └→ agent_fallback   → END
                  │                  ├→ clarify          → END
                  │                  ├→ template_fallback→ END
                  │                  └→ agent_fallback   → END
                  └→ agent_fallback  → END   (低置信度)

    所有降级 reason 经 ``edges._dispatch_bail_out`` 分发到 agent / clarify /
    template_fallback 三个节点，节点把结果写入 state 后连 END，
    ``handle_skill_mode`` 拿到 state 直接拼装 ChatResponse。
    """
    g = StateGraph()
    g.add_node("classify", classify_node)
    g.add_node("atlas", atlas_node)
    g.add_node("guide", guide_node)
    g.add_node("merge_filters", merge_filters_node)
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
    g.add_conditional_edge("merge_filters", after_merge_filters)
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


def _build_pipeline_a_stream_graph() -> StateGraph:
    """Pipeline A 完整图（SSE 流式版） — 与 ``_build_pipeline_a_graph`` 拓扑一致，
    仅把 atlas/guide/generate/agent_fallback 替换为 stream 节点。"""
    g = StateGraph()
    g.add_node("classify", classify_node)
    g.add_stream_node("atlas", atlas_stream_node)
    g.add_stream_node("guide", guide_stream_node)
    g.add_node("merge_filters", merge_filters_node)
    g.add_node("route", route_node)
    g.add_node("execute", execute_node)
    g.add_stream_node("generate", generate_stream_node)
    g.add_stream_node("agent_fallback", agent_fallback_stream_node)
    g.add_node("clarify", clarify_node)
    g.add_node("template_fallback", template_fallback_node)
    g.set_entry("classify")
    g.add_conditional_edge("classify", after_classify)
    g.add_edge("atlas", END)
    g.add_edge("guide", END)
    g.add_conditional_edge("merge_filters", after_merge_filters)
    g.add_conditional_edge("route", after_route)
    g.add_conditional_edge("execute", after_execute)
    g.add_edge("generate", END)
    g.add_edge("agent_fallback", END)
    g.add_edge("clarify", END)
    g.add_edge("template_fallback", END)
    return g


def _build_pipeline_direct_stream_graph() -> StateGraph:
    """Pipeline A skill_calls 直传短图（SSE 流式版）。"""
    g = StateGraph()
    g.add_node("execute", execute_node)
    g.add_stream_node("generate", generate_stream_node)
    g.add_stream_node("agent_fallback", agent_fallback_stream_node)
    g.add_node("clarify", clarify_node)
    g.set_entry("execute")
    g.add_conditional_edge("execute", after_execute)
    g.add_edge("generate", END)
    g.add_edge("agent_fallback", END)
    g.add_edge("clarify", END)
    return g


def _build_pipeline_confirmation_stream_graph() -> StateGraph:
    """confirmation_id 直达图（SSE 流式版）：仅 generate_stream → END。

    state.extras["executor_result"] 由调用方预填（含目标 servant/CE 单条），
    跳过 SkillExecutor 重复执行避免名称模糊匹配再次触发 clarification 死循环。
    """
    g = StateGraph()
    g.add_stream_node("generate", generate_stream_node)
    g.set_entry("generate")
    g.add_edge("generate", END)
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


def _get_pipeline_a_stream_graph() -> StateGraph:
    global _PIPELINE_A_STREAM_GRAPH
    if _PIPELINE_A_STREAM_GRAPH is None:
        _PIPELINE_A_STREAM_GRAPH = _build_pipeline_a_stream_graph()
    return _PIPELINE_A_STREAM_GRAPH


def _get_pipeline_direct_stream_graph() -> StateGraph:
    global _PIPELINE_DIRECT_STREAM_GRAPH
    if _PIPELINE_DIRECT_STREAM_GRAPH is None:
        _PIPELINE_DIRECT_STREAM_GRAPH = _build_pipeline_direct_stream_graph()
    return _PIPELINE_DIRECT_STREAM_GRAPH


def _get_pipeline_confirmation_stream_graph() -> StateGraph:
    global _PIPELINE_CONFIRMATION_STREAM_GRAPH
    if _PIPELINE_CONFIRMATION_STREAM_GRAPH is None:
        _PIPELINE_CONFIRMATION_STREAM_GRAPH = _build_pipeline_confirmation_stream_graph()
    return _PIPELINE_CONFIRMATION_STREAM_GRAPH


# ────────────────────────────────────────────────────────────
# Task 4 Batch B：全局 SessionStore（多轮对话 + 系统主动中断）
# ────────────────────────────────────────────────────────────

_SESSION_STORE: SessionStore | None = None


def _get_session_store() -> SessionStore:
    """惰性构造全局 SessionStore（SqliteCheckpointer 落地 server/data/checkpoints.db）。

    通过 ``LAPLACE_CHECKPOINT_DB`` 环境变量可覆盖路径（用于测试 / 容器卷映射）；
    默认路径与持久化数据共用 ``server/data/`` 目录，30 分钟 TTL（应用层惰性清理）。
    """
    global _SESSION_STORE
    if _SESSION_STORE is None:
        db_path = os.environ.get("LAPLACE_CHECKPOINT_DB")
        if not db_path:
            db_path = str(Path(__file__).parent / "data" / "checkpoints.db")
        _SESSION_STORE = SessionStore(SqliteCheckpointer(db_path))
    return _SESSION_STORE


def get_session_store() -> SessionStore:
    """对外暴露的 SessionStore 访问器（main.py 的 /chat/resume 路由复用）。"""
    return _get_session_store()


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
    session_id: str = "",
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
        session_id: 多轮对话会话 ID（前端 UUID）。非空时把 SessionStore 注入 state.extras，
            classify_node 会加载 prev_summary，generate_node 末尾会写 TurnSnapshot；
            为空时按单轮处理，行为完全等价于 Task 3。
    """
    if confirmation_context:
        user_message = f"{user_message}\n[用户确认：{confirmation_context}]"
    request_start = time.monotonic()
    bind_trace_id(trace_id)

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
            session_id=session_id,
        )
        if session_id:
            state.extras["session_store"] = _get_session_store()
        try:
            state = await _get_pipeline_direct_graph().run(state)
        except Exception as e:
            state.metric_labels["error_reason"] = "routing_error"
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - request_start) * 1000,
                    "result": "routing_error",
                    "mode": "routing_error",
                    "total_tokens": state.trace_total_tokens,
                    "metric_labels": dict(state.metric_labels),
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
        session_id=session_id,
    )
    state.extras["is_confirmation"] = confirmation_context is not None
    state.extras["confirmation_context_preview"] = confirmation_context[:200] if confirmation_context else None
    if session_id:
        state.extras["session_store"] = _get_session_store()

    try:
        state = await _get_pipeline_a_graph().run(state)
    except Exception as e:
        state.metric_labels["error_reason"] = "routing_error"
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - request_start) * 1000,
                "result": "routing_error",
                "mode": "routing_error",
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
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


async def resume_skill_mode(
    *,
    session_id: str,
    supplement_message: str,
    trace_id: str,
    client_ip: str = "unknown",
) -> ChatResponse:
    """从 pending checkpoint 恢复执行（用户补充答复后续做 — Task 4 Batch B）。

    工作流程：
    1. 从 SessionStore 加载 pending PipelineState；不存在或已过期则返回错误响应。
    2. 清掉 pending checkpoint，避免重复消费。
    3. 把用户原问题作为 ``confirmation_context``，新答复作为 ``user_message``，
       重新调用 ``handle_skill_mode`` 走一次完整管线（与现有 confirmation_context
       机制对齐，路由层会把上下文拼接到 prompt）。
    4. 多轮链路（session_id）保持，下一轮 prev_summary 仍可命中。

    Args:
        session_id: 前端 UUID，必须与系统 yield pending_question 时的 session_id 一致。
        supplement_message: 用户补充的答复文本（如选择某个 clarification 选项）。
        trace_id: 本次 resume 的 trace_id（与原中断 trace 不同，便于审计）。
        client_ip: 客户端 IP，用于日志。
    """
    store = _get_session_store()
    pending = store.load_pending(session_id)
    if pending is None:
        await log_trace_event(
            trace_id,
            "resume_no_pending",
            {"session_id": session_id},
        )
        return ChatResponse(
            reply="对话已超时或不存在待恢复的状态，请重新开始一次提问。",
            servants=[],
            count=0,
            query={"error": "no_pending", "session_id": session_id},
            model="error",
            traceId=trace_id,
        )
    # 取出原始用户输入作为 confirmation_context
    original_message = getattr(pending, "user_message", "") or ""
    store.clear_pending(session_id)
    await log_trace_event(
        trace_id,
        "resume_loaded",
        {
            "session_id": session_id,
            "original_message_preview": original_message[:200],
            "supplement_preview": supplement_message[:200],
        },
    )
    return await handle_skill_mode(
        user_message=supplement_message,
        trace_id=trace_id,
        client_ip=client_ip,
        confirmation_context=original_message or None,
        session_id=session_id,
    )


# ====================================================================
# Task 5 — SSE 流式入口（统一图引擎驱动 — ADR-028 终态）
# ====================================================================


async def stream_chat_events(
    message: str,
    preset_name: str | None = None,
    *,
    client_ip: str = "unknown",
    confirmation_context: str | None = None,
    confirmation_id: str | None = None,
    session_id: str = "",
):
    """SSE 流式入口（Task 5）— 三套图分发：confirmation_id / preset / 完整 Pipeline A。

    旧 ``stream_event_generator`` 已被本函数替代；事件契约和顺序保持一致：
    每条 SSE event 由 ``sse_event(type, data)`` 序列化；最终一定 yield 一条 done 事件。

    Args:
        confirmation_context: 用户确认选择后回传的上下文，拼接到 message 进行精确路由。
        confirmation_id: 用户选择的选项 ID（collectionNo），用于精确定位实体跳过路由。
        session_id: 多轮对话会话 ID（前端 UUID）。非空时把 SessionStore 注入 state.extras。
    """
    trace_id = uuid.uuid4().hex[:8]
    stream_start = time.monotonic()
    bind_trace_id(trace_id)

    # ── 路径 1：confirmation_id 直达（用户选择具体实体，跳过路由+执行）──
    if confirmation_id and confirmation_id.isdigit():
        async for ev in _stream_confirmation_direct(
            message=message,
            confirmation_id=int(confirmation_id),
            confirmation_context=confirmation_context or "",
            trace_id=trace_id,
            stream_start=stream_start,
            client_ip=client_ip,
            session_id=session_id,
        ):
            yield ev
        return

    # 用户确认后的第二次请求（自定义输入，无 confirmation_id）：拼接上下文到消息
    if confirmation_context:
        message = f"{message}\n[用户确认：{confirmation_context}]"

    # ── 路径 2：preset_name 直传（跳过 LLM 路由，直接展开预设）──
    if preset_name:
        async for ev in _stream_preset(
            message=message,
            preset_name=preset_name,
            trace_id=trace_id,
            stream_start=stream_start,
            client_ip=client_ip,
            session_id=session_id,
        ):
            yield ev
        return

    # ── 路径 3：完整 Pipeline A 流式图（Stage 0 → Stage 1 → execute → generate）──
    state = PipelineState(
        user_message=message,
        trace_id=trace_id,
        request_start=stream_start,
        client_ip=client_ip,
        model_used="skill_mode",
        session_id=session_id,
    )
    state.extras["streaming"] = True
    state.extras["is_confirmation"] = confirmation_context is not None
    state.extras["confirmation_context_preview"] = confirmation_context[:200] if confirmation_context else None
    if session_id:
        state.extras["session_store"] = _get_session_store()

    await log_trace_event(
        trace_id,
        "routing_input",
        {
            "query": message,
            "mode": "two_stage",
            "skill_count": sum(1 for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)),
            "client_ip": client_ip,
            "is_confirmation": confirmation_context is not None,
            "confirmation_context": confirmation_context[:200] if confirmation_context else None,
        },
    )

    try:
        async for produced in _get_pipeline_a_stream_graph().run_stream(state):
            yield sse_event(produced["type"], produced["data"])
    except Exception as e:  # noqa: BLE001
        state.metric_labels["error_reason"] = "stream_error"
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - stream_start) * 1000,
                "result": "stream_error",
                "mode": "stream_error",
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
            error=str(e),
        )
        yield sse_event("error", {"message": "服务异常，请稍后重试"})
        yield sse_event("done", {"model": "error", "traceId": trace_id})
        return

    yield sse_event("done", _build_done_data(state, trace_id))


def _build_done_data(state: PipelineState, trace_id: str) -> dict:
    """拼装 done payload：含 model / traceId，并按需追加 needs_confirmation / rejected_skills。"""
    data: dict = {"model": state.model_used or "unknown", "traceId": trace_id}

    # clarification 路径（routing/execution 层）：query.mode=clarification → 附 needs_confirmation
    if isinstance(state.query, dict) and state.query.get("mode") == "clarification":
        data["needs_confirmation"] = True

    result = state.extras.get("executor_result")
    if result is not None and getattr(result, "rejected_skills", None):
        data["rejected_skills"] = result.rejected_skills

    return data


async def _stream_confirmation_direct(
    *,
    message: str,
    confirmation_id: int,
    confirmation_context: str,
    trace_id: str,
    stream_start: float,
    client_ip: str,
    session_id: str = "",
):
    """confirmation_id 直达路径 — 跳过 SkillExecutor，预填 ExecutionResult 后走
    ``_get_pipeline_confirmation_stream_graph()`` 仅生成节点的短图。

    避免名称模糊匹配再次触发 clarification 死循环（ADR-023 教训）。
    事件顺序：thinking routing → thinking routed → thinking querying →
    （由 generate_stream_node 接管）servants → thinking generating → delta * N → done。
    """
    bind_trace_id(trace_id)
    from server.query_executor import load_ce_database, load_database

    yield sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})

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

    # 按 collectionNo 在两个数据库中查找
    target = None
    domain = "servant"
    for s in load_database():
        if s.get("collectionNo") == confirmation_id:
            target = s
            break
    if target is None:
        for ce in load_ce_database():
            if ce.get("collectionNo") == confirmation_id:
                target = ce
                domain = "ce"
                break

    if target is None:
        yield sse_event("error", {"message": f"未找到编号 {confirmation_id} 的实体"})
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - stream_start) * 1000,
                "result": "confirmation_not_found",
                "metric_labels": {
                    "pipeline": "confirmation",
                    "error_reason": "confirmation_not_found",
                },
            },
        )
        yield sse_event("done", {"model": "confirmation_direct", "traceId": trace_id})
        return

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
    await log_trace_event(
        trace_id,
        "execution",
        {"accepted_skills": skill_calls, "total_found": 1, "confirmation_direct": True},
    )

    yield sse_event("thinking", {"phase": "querying", "message": "正在检索从者数据..."})

    # 预填 ExecutionResult，走 confirmation_stream_graph（仅 generate_stream_node）
    response_skill_obj = SKILL_REGISTRY.get(response_skill_name)
    if not isinstance(response_skill_obj, ResponseSkill):
        response_skill_obj = None

    pre_result = ExecutionResult(
        servants=[target],
        total_found=1,
        response_skill=response_skill_obj,
        accepted_skills=skill_calls,
    )

    state = PipelineState(
        user_message=message,
        trace_id=trace_id,
        request_start=stream_start,
        client_ip=client_ip,
        model_used="confirmation_direct",
        skill_calls=skill_calls,
        response_skill_name=response_skill_name,
        session_id=session_id,
    )
    state.extras["executor_result"] = pre_result
    state.extras["streaming"] = True
    if session_id:
        state.extras["session_store"] = _get_session_store()

    try:
        async for produced in _get_pipeline_confirmation_stream_graph().run_stream(state):
            yield sse_event(produced["type"], produced["data"])
    except Exception as e:  # noqa: BLE001
        state.metric_labels["error_reason"] = "confirmation_stream_error"
        state.metric_labels.setdefault("pipeline", "confirmation")
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - stream_start) * 1000,
                "result": "confirmation_stream_error",
                "mode": "confirmation_direct",
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
            error=str(e),
        )
        yield sse_event("error", {"message": "服务异常，请稍后重试"})
        yield sse_event("done", {"model": state.model_used or "confirmation_direct", "traceId": trace_id})
        return

    yield sse_event(
        "done",
        {"model": state.model_used or "confirmation_direct", "traceId": trace_id},
    )


async def _stream_preset(
    *,
    message: str,
    preset_name: str,
    trace_id: str,
    stream_start: float,
    client_ip: str,
    session_id: str = "",
):
    """preset_name 直传路径 — 跳过 LLM 路由，按 preset 展开 skill_calls，走
    ``_get_pipeline_direct_stream_graph()`` 短图（execute → generate）。

    支持 B1 策略：用户补充文字时通过 Stage 2 LLM 合并额外 skill_calls。
    """
    bind_trace_id(trace_id)
    yield sse_event("thinking", {"phase": "routing", "message": "正在解析预设..."})

    preset = PRESET_REGISTRY.get(preset_name)
    if preset is None:
        yield sse_event("error", {"phase": "routing", "message": f"未知的预设：{preset_name}"})
        yield sse_event("done", {"model": "error", "traceId": trace_id})
        return

    response_skill_name = preset.response_skill
    skill_calls: list[dict] = []
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
            existing_map = {s["skill_name"]: s for s in skill_calls}
            for es in extra_skills:
                es_name = es.get("skill_name")
                if es_name in existing_map:
                    for k, v in es.get("params", {}).items():
                        if k not in existing_map[es_name]["params"]:
                            existing_map[es_name]["params"][k] = v
                else:
                    skill_calls.append(es)
                    existing_map[es_name] = es
            extra_resp_skill = extra_routing.get("response_skill")
            if extra_resp_skill and extra_resp_skill != "respond_servant_list":
                response_skill_name = extra_resp_skill
        except Exception:  # noqa: BLE001
            # 补充解析失败不影响预设查询（静默）
            pass

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

    state = PipelineState(
        user_message=message,
        trace_id=trace_id,
        request_start=stream_start,
        client_ip=client_ip,
        model_used=model_used,
        skill_calls=skill_calls,
        response_skill_name=response_skill_name,
        target_pipeline="A",
        session_id=session_id,
    )
    state.extras["streaming"] = True
    if session_id:
        state.extras["session_store"] = _get_session_store()

    try:
        async for produced in _get_pipeline_direct_stream_graph().run_stream(state):
            yield sse_event(produced["type"], produced["data"])
    except Exception as e:  # noqa: BLE001
        state.metric_labels["error_reason"] = "preset_stream_error"
        state.metric_labels.setdefault("pipeline", "preset")
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": (time.monotonic() - stream_start) * 1000,
                "result": "preset_stream_error",
                "mode": "preset",
                "total_tokens": state.trace_total_tokens,
                "metric_labels": dict(state.metric_labels),
            },
            error=str(e),
        )
        yield sse_event("error", {"message": "服务异常，请稍后重试"})
        yield sse_event("done", {"model": "error", "traceId": trace_id})
        return

    yield sse_event("done", _build_done_data(state, trace_id))


# ============================================================
# 链路 B/C 处理函数（JSON 模式 wrapper — 仍被 tests/test_pipeline_bc_equivalence.py 引用）
# ============================================================


async def _handle_atlas_pipeline(
    user_message: str,
    trace_id: str,
    model_used: str,
    request_start: float,
    trace_total_tokens: int,
    atlas_query: dict | None = None,
) -> ChatResponse:
    """链路 B：Atlas 知识问答（JSON 模式）。Task 1 起改为图引擎执行。

    本函数是 thin wrapper：构建 PipelineState → graph.run(atlas_node) → 拼装 ChatResponse。
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
    """链路 C：攻略知识问答（JSON 模式）。Task 1 起改为图引擎执行（非流式）。

    SSE 流式版本由 ``stream_chat_events`` 内部直接调用 ``guide_stream_node``（Task 5 起）。
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
