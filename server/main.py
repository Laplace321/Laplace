"""
Laplace — FastAPI Server (Skill-Based Architecture)

对话式 FGO 数据查询 API。
支持 natural_language（两阶段 LLM 路由）和 preset（快捷查询）双模式。
支持传统 JSON 端点和 SSE 流式端点。
"""

import json
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# 预消化翻译字典（从 config/translations.json 加载，支持热更新）
from server.config_loader import CachedConfig
from server.llm_client import chat_completion
from server.logger import find_trace, log_chat_trace_async, read_traces
from server.prompts import (  # DEPRECATED 兼容
    build_params_prompt,
    build_routing_prompt,
    get_generation_prompt,
)
from server.query_executor import load_database
from server.rate_limiter import RateLimitMiddleware
from server.schemas import ChatRequest, ChatResponse
from server.skills import *  # noqa: F401,F403 — 触发 @register_skill 注册
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.skills.executor import ExecutionResult, SkillExecutor
from server.skills.presets import PRESET_REGISTRY

_translations_cache = CachedConfig(Path(__file__).parent / "config" / "translations.json")


def _get_class_map() -> dict:
    return _translations_cache.get()["className"]


def _get_np_card_map() -> dict:
    return _translations_cache.get()["npCard"]


def _get_np_target_map() -> dict:
    return _translations_cache.get()["npTarget"]


_effect_map = None


def get_effect_translation(effect_code: str) -> str:
    global _effect_map
    if _effect_map is None:
        _effect_map = {}
        schema_path = Path(__file__).parent / "knowledge" / "effect_schema.json"
        if schema_path.exists():
            with open(schema_path, encoding="utf-8") as f:
                data = json.load(f)
                for effect in data.get("effects", []):
                    name = effect.get("name")
                    aliases = effect.get("aliases_zh", [])
                    if name and aliases:
                        _effect_map[name] = aliases[0]
    return _effect_map.get(effect_code, effect_code)


# === 共享业务逻辑 ===

MAX_CONTEXT_SIZE = 5
MAX_RESULTS = 50


def _build_context(servants: list[dict]) -> tuple[dict, list[dict]]:
    """构建预消化的精简 Context 供 RAG 生成使用。

    Returns:
        (context_data, top_results) — context_data 含 total_found 等元信息；
        top_results 为翻译后的前 N 条详情。
    """
    total_found = len(servants)
    top_results = []

    for s in servants[:MAX_CONTEXT_SIZE]:
        raw_np_card = s.get("npCard")
        raw_np_target = s.get("npTarget")
        raw_class_name = s.get("className")
        raw_effects = s.get("skillEffects") or []
        raw_np_effects = s.get("npEffects") or []

        translated_effects = [get_effect_translation(e) for e in raw_effects]
        translated_np_effects = [get_effect_translation(e) for e in raw_np_effects]

        card_buff_status = []
        if "upArts" in raw_effects:
            card_buff_status.append("有蓝卡提升")
        else:
            card_buff_status.append("无蓝卡提升")
        if "upQuick" in raw_effects:
            card_buff_status.append("有绿卡提升")
        else:
            card_buff_status.append("无绿卡提升")
        if "upBuster" in raw_effects:
            card_buff_status.append("有红卡提升")
        else:
            card_buff_status.append("无红卡提升")

        top_results.append(
            {
                "name": s.get("name"),
                "aliasCN": s.get("aliasCN"),
                "className": _get_class_map().get(str(raw_class_name).lower(), raw_class_name),
                "rarity": s.get("rarity"),
                "totalCharge": s.get("totalCharge"),
                "npCard": _get_np_card_map().get(str(raw_np_card).lower(), raw_np_card),
                "npTarget": _get_np_target_map().get(str(raw_np_target).lower(), raw_np_target),
                "skillEffects": translated_effects,
                "npEffects": translated_np_effects,
                "__internal_card_buff_check": " | ".join(card_buff_status),
            }
        )

    return {
        "total_found": total_found,
        "query_conditions": {},  # 由调用方填充
        "top_results_details": top_results,
    }, top_results


def _sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 事件。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


app = FastAPI(
    title="Laplace API",
    description="AI Native FGO 数据助手",
    version="0.2.0",
)

# CORS — 从环境变量读取白名单（默认仅本地开发）
_default_origins = "http://localhost:8000,http://127.0.0.1:8000"
cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate Limit — 保护 LLM quota（双层：Per-IP + Global）
_rate_limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))
_rate_limit_global = int(os.getenv("RATE_LIMIT_GLOBAL_PER_MINUTE", "100"))
app.add_middleware(
    RateLimitMiddleware,
    max_requests=_rate_limit,
    global_max_requests=_rate_limit_global,
    paths=["/api/chat", "/api/chat/stream"],
)


# ChatRequest / ChatResponse 从 server.schemas 导入（见文件顶部）

# === Skill 架构辅助函数 ===

_skill_executor = SkillExecutor()


def _get_skill_descriptions() -> list[dict[str, str]]:
    """构建供路由 Prompt 使用的 Skill 描述列表。"""
    return [
        {"name": skill.name, "description": skill.description}
        for skill in SKILL_REGISTRY.values()
        if isinstance(skill, QuerySkill)
    ]


async def _handle_natural_language(user_message: str, trace_id: str) -> tuple[ChatResponse, str]:
    """natural_language 模式：两阶段 LLM 路由 + Skill 执行。

    Returns:
        (response, model_used)
    """
    model_used = "unknown"

    # Stage 1: 路由 — 选择 Skills
    routing_prompt = build_routing_prompt(_get_skill_descriptions())
    try:
        parsed = await chat_completion(
            system_prompt=routing_prompt,
            user_message=user_message,
            temperature=0.1,
            json_mode=True,
        )
    except Exception as e:
        print(f"[{trace_id}] Stage 1 Routing Error: {e}")
        await log_chat_trace_async(
            trace_id,
            user_message,
            {},
            0,
            "路由阶段失败",
            error=str(e),
        )
        return ChatResponse(
            reply="抱歉，我遇到了网络问题或模型暂时不可用，请稍后再试。",
            servants=[],
            count=0,
            query={},
            model="error",
            traceId=trace_id,
        ), "error"

    model_used = parsed.pop("_model", "unknown")
    parsed.pop("_response_format", None)

    # 检查路由 fallback（第一层兜底）
    fallback = parsed.get("fallback")
    if fallback is not None:
        fallback_msg = fallback.get("message", "抱歉，我目前只能回答 FGO 从者相关的问题。")
        await log_chat_trace_async(trace_id, user_message, parsed, 0, fallback_msg)
        return ChatResponse(
            reply=fallback_msg,
            servants=[],
            count=0,
            query=parsed,
            model=model_used,
            traceId=trace_id,
        ), model_used

    # Stage 2: 参数填充
    skill_calls = parsed.get("query_skills", [])
    response_skill_name = parsed.get("response_skill", "respond_servant_list")

    if skill_calls:
        params_prompt = build_params_prompt(skill_calls, user_message, SKILL_REGISTRY)
        try:
            params_parsed = await chat_completion(
                system_prompt=params_prompt,
                user_message=user_message,
                temperature=0.1,
                json_mode=True,
            )
            model_used = params_parsed.pop("_model", model_used)
            params_parsed.pop("_response_format", None)

            # 解析参数填充结果
            filled_calls = (
                params_parsed if isinstance(params_parsed, list) else params_parsed.get("skills", skill_calls)
            )
            if isinstance(filled_calls, list):
                skill_calls = filled_calls
        except Exception as e:
            print(f"[{trace_id}] Stage 2 Params Error: {e}")
            # 参数填充失败不阻断，使用空参数继续

    # 执行 Skills
    result = _skill_executor.execute(skill_calls, response_skill_name)
    response = await _build_chat_response(
        result,
        user_message,
        skill_calls,
        model_used,
        trace_id,
    )
    return response, model_used


async def _handle_preset(request: ChatRequest, trace_id: str) -> tuple[ChatResponse, str]:
    """preset 模式：表单参数直接实例化 + 可选 supplement 走 Stage 2。

    B1 策略：表单参数固定，supplement 仅送 Stage 2 解析出额外 Skill。
    """
    model_used = "none"
    preset_name = request.preset_name or ""
    preset = PRESET_REGISTRY.get(preset_name)

    if preset is None:
        return ChatResponse(
            reply=f"未知的预设查询：{preset_name}",
            servants=[],
            count=0,
            query={"preset": preset_name},
            model="none",
            traceId=trace_id,
        ), "none"

    # 从表单参数构建 SkillCall 列表
    user_params = request.params or {}
    skill_calls = []
    for skill_name in preset.query_skills:
        params = user_params.get(skill_name, preset.param_template.get(skill_name, {}))
        skill_calls.append({"skill_name": skill_name, "params": params})

    response_skill_name = request.response_skill or preset.response_skill

    # B1 策略：supplement 非空 → 送 Stage 2 解析出额外 Skill
    supplement = (request.supplement or "").strip()
    if supplement:
        params_prompt = build_params_prompt(
            [{"skill_name": s, "params": {}} for s in SKILL_REGISTRY if isinstance(SKILL_REGISTRY[s], QuerySkill)],
            supplement,
            SKILL_REGISTRY,
        )
        try:
            params_parsed = await chat_completion(
                system_prompt=params_prompt,
                user_message=supplement,
                temperature=0.1,
                json_mode=True,
            )
            model_used = params_parsed.pop("_model", "unknown")
            params_parsed.pop("_response_format", None)

            extra_calls = params_parsed if isinstance(params_parsed, list) else []
            if isinstance(extra_calls, list):
                # 合并：表单参数优先，额外 Skill 追加
                existing_names = {c["skill_name"] for c in skill_calls}
                for call in extra_calls:
                    if isinstance(call, dict) and call.get("skill_name") not in existing_names:
                        skill_calls.append(call)
        except Exception as e:
            print(f"[{trace_id}] Preset supplement Stage 2 Error: {e}")
            # supplement 解析失败不阻断

    # 执行 Skills
    user_message = supplement or request.message or preset.display_name
    result = _skill_executor.execute(skill_calls, response_skill_name)
    response = await _build_chat_response(
        result,
        user_message,
        skill_calls,
        model_used,
        trace_id,
    )
    return response, model_used


async def _build_chat_response(
    result: ExecutionResult,
    user_message: str,
    skill_calls: list[dict],
    model_used: str,
    trace_id: str,
) -> ChatResponse:
    """从 ExecutionResult 构建 ChatResponse，包含 RAG 生成。"""
    # 执行阶段兜底
    if result.is_fallback:
        fallback_reply = result.fallback_message or "未找到匹配结果。"
        await log_chat_trace_async(
            trace_id,
            user_message,
            {"skill_calls": skill_calls},
            0,
            fallback_reply,
        )
        return ChatResponse(
            reply=fallback_reply,
            servants=[],
            count=0,
            query={"skill_calls": skill_calls},
            model=model_used,
            traceId=trace_id,
        )

    # 构建 Context + RAG 生成
    servants = result.servants
    total_found = result.total_found
    context_data, _ = _build_context(servants)
    context_data["query_conditions"] = {"skill_calls": skill_calls}

    # 使用 Response Skill 的 generation_prompt
    response_skill = result.response_skill
    if response_skill is not None:
        generation_prompt = response_skill.build_prompt(
            user_message,
            json.dumps(context_data, ensure_ascii=False),
        )
    else:
        generation_prompt = get_generation_prompt(
            user_message,
            json.dumps(context_data, ensure_ascii=False),
        )

    try:
        generation_response = await chat_completion(
            system_prompt=(
                "You are a helpful AI assistant. You MUST strictly follow "
                "the provided data and NEVER use your internal knowledge about FGO."
            ),
            user_message=generation_prompt,
            temperature=0.1,
            json_mode=False,
        )
        final_reply = generation_response.get("text", "").strip()
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as e:
        print(f"[{trace_id}] RAG Generate Error: {e}")
        final_reply = f"为你找到了 {total_found} 位从者，请查看卡片详情。"

    # 日志
    await log_chat_trace_async(
        trace_id=trace_id,
        user_message=user_message,
        parsed_intent={"skill_calls": skill_calls},
        found_count=total_found,
        final_reply=final_reply,
        context=context_data,
    )

    returned_servants = servants[:MAX_RESULTS]
    return ChatResponse(
        reply=final_reply,
        servants=returned_servants,
        count=total_found,
        query={"skill_calls": skill_calls},
        model=model_used,
        traceId=trace_id,
    )


def _is_localhost(request: Request) -> bool:
    """检查请求是否来自本地。"""
    return request.client.host in ("127.0.0.1", "::1", "localhost")


@app.get("/api/traces")
async def list_traces(request: Request, limit: int = 20):
    """返回最近 N 条 trace（仅 localhost 可访问）。"""
    if not _is_localhost(request):
        return JSONResponse(status_code=403, content={"error": "仅允许本地访问"})
    traces = read_traces(limit=limit)
    return traces


@app.get("/api/traces/{trace_id}")
async def get_trace(request: Request, trace_id: str):
    """按 trace_id 查询单条 trace 详情（仅 localhost 可访问）。"""
    if not _is_localhost(request):
        return JSONResponse(status_code=403, content={"error": "仅允许本地访问"})
    trace = find_trace(trace_id)
    if trace is None:
        return JSONResponse(status_code=404, content={"error": f"trace {trace_id} 未找到"})
    return trace


def _validate_translations():
    """校验 config/translations.json 与 knowledge/class_mapping.json 的一致性。

    检查翻译映射是否覆盖了所有可玩职阶，防止预消化翻译与知识库脱节。
    不一致时输出警告日志，不阻塞启动。
    """
    knowledge_path = Path(__file__).parent / "knowledge" / "class_mapping.json"
    if not knowledge_path.exists():
        print("⚠️  knowledge/class_mapping.json 不存在，跳过翻译一致性校验")
        return

    with open(knowledge_path, encoding="utf-8") as f:
        class_mapping = json.load(f)

    # 从知识库提取可玩职阶名（全小写）
    playable_classes = {entry["name"].lower() for entry in class_mapping.get("playable", [])}

    # 从翻译配置提取已有翻译的职阶名（全小写）
    translated_classes = {k.lower() for k in _get_class_map().keys()}

    missing = playable_classes - translated_classes
    if missing:
        print(f"⚠️  翻译映射缺失：以下可玩职阶在 config/translations.json 中没有中文翻译: {sorted(missing)}")
        print("   请在 server/config/translations.json 的 className 中补充对应翻译")

    extra = translated_classes - playable_classes
    if extra:
        # 额外的翻译不是错误（如 beast），仅做信息提示
        print(f"ℹ️  翻译映射包含非可玩职阶（可忽略）: {sorted(extra)}")


@app.on_event("startup")
async def startup():
    """启动时预加载数据库并校验配置一致性。"""
    load_database()
    _validate_translations()


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """处理用户对话请求（双模式：natural_language / preset）。"""
    trace_id = uuid.uuid4().hex[:8]

    if request.mode == "preset":
        response, _ = await _handle_preset(request, trace_id)
    else:
        user_message = request.message
        if not user_message or not user_message.strip():
            return ChatResponse(
                reply="请输入你的问题。",
                servants=[],
                count=0,
                query={},
                model="none",
                traceId=trace_id,
            )
        response, _ = await _handle_natural_language(user_message, trace_id)

    return response


@app.get("/api/chat/stream")
async def chat_stream(message: str):
    """SSE 流式对话端点 — 两阶段 LLM 路由 + Skill 执行。

    注意：SSE 端点当前仅支持 natural_language 模式。
    preset 模式通过 POST /api/chat 使用。
    """

    async def event_generator():
        trace_id = uuid.uuid4().hex[:8]
        model_used = "unknown"

        # ── 阶段 1: 路由 — 选择 Skills ──
        yield _sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})

        routing_prompt = build_routing_prompt(_get_skill_descriptions())
        try:
            parsed = await chat_completion(
                system_prompt=routing_prompt,
                user_message=message,
                temperature=0.1,
                json_mode=True,
            )
        except Exception as e:
            print(f"[{trace_id}] Stage 1 Routing Error: {e}")
            await log_chat_trace_async(trace_id, message, {}, 0, "路由阶段失败", error=str(e))
            yield _sse_event("error", {"phase": "routing", "message": "意图解析失败，请稍后重试"})
            return

        model_used = parsed.pop("_model", "unknown")
        parsed.pop("_response_format", None)

        # 检查路由 fallback
        fallback = parsed.get("fallback")
        if fallback is not None:
            fallback_msg = fallback.get("message", "抱歉，我目前只能回答 FGO 从者相关的问题。")
            await log_chat_trace_async(trace_id, message, parsed, 0, fallback_msg)
            yield _sse_event("delta", {"text": fallback_msg})
            yield _sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        skill_calls = parsed.get("query_skills", [])
        response_skill_name = parsed.get("response_skill", "respond_servant_list")

        # 推送路由结果
        yield _sse_event(
            "thinking",
            {
                "phase": "routed",
                "skills": [c.get("skill_name", "") for c in skill_calls],
                "response_skill": response_skill_name,
            },
        )

        # ── 阶段 2: 参数填充 ──
        if skill_calls:
            yield _sse_event("thinking", {"phase": "filling_params", "message": "正在解析查询参数..."})

            params_prompt = build_params_prompt(skill_calls, message, SKILL_REGISTRY)
            try:
                params_parsed = await chat_completion(
                    system_prompt=params_prompt,
                    user_message=message,
                    temperature=0.1,
                    json_mode=True,
                )
                model_used = params_parsed.pop("_model", model_used)
                params_parsed.pop("_response_format", None)

                filled_calls = (
                    params_parsed if isinstance(params_parsed, list) else params_parsed.get("skills", skill_calls)
                )
                if isinstance(filled_calls, list):
                    skill_calls = filled_calls
            except Exception as e:
                print(f"[{trace_id}] Stage 2 Params Error: {e}")
                # 参数填充失败不阻断

        # ── 阶段 3: 数据检索 ──
        yield _sse_event("thinking", {"phase": "querying", "message": "正在检索从者数据..."})

        result = _skill_executor.execute(skill_calls, response_skill_name)

        # 执行阶段兜底
        if result.is_fallback:
            fallback_reply = result.fallback_message or "未找到匹配结果。"
            await log_chat_trace_async(
                trace_id,
                message,
                {"skill_calls": skill_calls},
                0,
                fallback_reply,
            )
            yield _sse_event("delta", {"text": fallback_reply})
            yield _sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        servants = result.servants
        total_found = result.total_found
        returned_servants = servants[:MAX_RESULTS]

        # 卡片先行 — 立即推送从者数据
        yield _sse_event(
            "servants",
            {
                "servants": returned_servants,
                "count": len(returned_servants),
                "total": total_found,
            },
        )

        # ── 阶段 4: RAG 生成 ──
        yield _sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

        context_data, _ = _build_context(servants)
        context_data["query_conditions"] = {"skill_calls": skill_calls}

        response_skill = result.response_skill
        if response_skill is not None:
            generation_prompt = response_skill.build_prompt(
                message,
                json.dumps(context_data, ensure_ascii=False),
            )
        else:
            generation_prompt = get_generation_prompt(
                message,
                json.dumps(context_data, ensure_ascii=False),
            )

        try:
            generation_response = await chat_completion(
                system_prompt=(
                    "You are a helpful AI assistant. You MUST strictly follow "
                    "the provided data and NEVER use your internal knowledge about FGO."
                ),
                user_message=generation_prompt,
                temperature=0.1,
                json_mode=False,
            )
            final_reply = generation_response.get("text", "").strip()
            if not final_reply:
                raise ValueError("Empty response from LLM")
        except Exception as e:
            print(f"[{trace_id}] RAG Generate Error: {e}")
            final_reply = f"为你找到了 {total_found} 位从者，请查看卡片详情。"

        # 推送生成的文本
        yield _sse_event("delta", {"text": final_reply})

        # 日志
        await log_chat_trace_async(
            trace_id=trace_id,
            user_message=message,
            parsed_intent={"skill_calls": skill_calls},
            found_count=total_found,
            final_reply=final_reply,
            context=context_data,
        )

        # ── 完成 ──
        yield _sse_event("done", {"model": model_used, "traceId": trace_id})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/health")
async def health():
    """健康检查。"""
    return {"status": "ok", "service": "laplace"}


# 挂载前端静态文件目录
app.mount("/", StaticFiles(directory="demo", html=True), name="static")
