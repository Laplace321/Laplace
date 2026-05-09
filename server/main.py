"""
Laplace — FastAPI Server

对话式 FGO 数据查询 API。
支持传统 JSON 端点和 SSE 流式端点。
"""

import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 预消化翻译字典（从 config/translations.json 加载，支持热更新）
from server.config_loader import CachedConfig
from server.llm_client import chat_completion
from server.logger import find_trace, log_trace_event, read_traces
from server.prompts import build_routing_prompt, get_generation_prompt
from server.query_executor import load_database
from server.rate_limiter import RateLimitMiddleware
from server.schemas import parse_routing_response, routing_response_json_schema
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.skills.executor import SkillExecutor
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


def _describe_filters(skill_calls: list[dict]) -> list[str]:
    """将 skill_calls 转换为人类可读的筛选条件描述列表。

    例如: ["技能效果包含「Arts提升」", "稀有度 = 5星"]
    供 LLM 在生成回复时明确知道系统做了什么筛选。
    """
    descriptions: list[str] = []
    for call in skill_calls:
        name = call.get("skill_name", "")
        params = call.get("params", {})
        if name == "search_by_skill_effect":
            effect = params.get("skillEffect") or params.get("effect", "")
            translated = get_effect_translation(effect) if effect else effect
            descriptions.append(f"技能效果包含「{translated}」")
        elif name == "search_by_np_effect":
            effect = params.get("npEffect") or params.get("effect", "")
            translated = get_effect_translation(effect) if effect else effect
            descriptions.append(f"宝具效果包含「{translated}」")
        elif name == "search_by_rarity":
            op = params.get("op", "eq")
            val = params.get("value", "")
            op_map = {"eq": "=", "gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
            descriptions.append(f"稀有度 {op_map.get(op, op)} {val}星")
        elif name == "search_by_class":
            descriptions.append(f"职阶 = {params.get('className', '')}")
        elif name == "search_by_np_charge":
            op = params.get("op", "gte")
            val = params.get("value", "")
            op_map = {"eq": "=", "gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
            descriptions.append(f"NP充能 {op_map.get(op, op)} {val}%")
        elif name == "search_by_cards":
            card = params.get("cardType", "")
            descriptions.append(f"配卡包含「{card}」")
        elif name == "search_by_traits":
            trait = params.get("trait", "")
            descriptions.append(f"特性包含「{trait}」")
        elif name == "search_by_attribute":
            attr = params.get("attribute", "")
            descriptions.append(f"属性 = {attr}")
        else:
            descriptions.append(f"{name}({params})")
    return descriptions


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

        top_results.append(
            {
                "name": s.get("name"),
                "aliasCN": s.get("aliasCN"),
                "className": _get_class_map().get(str(raw_class_name).lower(), raw_class_name),
                "rarity": s.get("rarity"),
                "cards": s.get("cards"),
                "totalCharge": s.get("totalCharge"),
                "npCard": _get_np_card_map().get(str(raw_np_card).lower(), raw_np_card),
                "npTarget": _get_np_target_map().get(str(raw_np_target).lower(), raw_np_target),
                "skillEffects": translated_effects,
                "npEffects": translated_np_effects,
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


class ChatRequest(BaseModel):
    """对话请求（Skill-Based Architecture）。"""

    message: str
    mode: str = "skill"
    preset_name: str | None = None
    params: dict | list | None = None
    response_skill: str | None = None


class ChatResponse(BaseModel):
    """对话响应。"""

    reply: str
    servants: list[dict]
    count: int
    query: dict
    model: str
    traceId: str | None = None


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


async def _handle_skill_mode(
    user_message: str,
    trace_id: str,
    skill_calls: list[dict] | None = None,
    response_skill_name: str = "respond_servant_list",
) -> ChatResponse:
    """Skill 模式的核心处理逻辑。

    接收已确定的 skill_calls（来自 LLM 路由或前端直传），
    执行 SkillExecutor 并生成 RAG 回复。
    """
    request_start = time.monotonic()
    executor = SkillExecutor()
    model_used = "skill_mode"

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
                "routing_prompt_length": len(routing_prompt),
                "skill_count": len(skill_descriptions),
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
                },
            )

            # 检查 fallback
            fallback = routing_result.get("fallback")
            if fallback is not None:
                fb_msg = fallback.get("message", "无法理解你的问题，请尝试更具体的描述。")
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - request_start) * 1000,
                        "result": "fallback",
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

            # 空 skill_calls 且无 fallback — LLM 未能识别意图
            if not skill_calls:
                no_match_msg = "无法从你的问题中识别出查询条件，请尝试更具体的描述。"
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - request_start) * 1000,
                        "result": "no_match",
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

    # Fallback 处理
    if result.is_fallback:
        final_reply = result.fallback_message or "未找到匹配的从者。"
    else:
        # RAG 生成
        context_data, _ = _build_context(servants)
        context_data["query_conditions"] = {"skill_calls": skill_calls}
        context_data["applied_filters"] = _describe_filters(skill_calls)
        context_json = json.dumps(context_data, ensure_ascii=False)

        # ── Trace: context_build ──
        await log_trace_event(
            trace_id,
            "context_build",
            {
                "applied_filters": context_data["applied_filters"],
                "top_results_count": len(context_data.get("top_results_details", [])),
                "context_json_length": len(context_json),
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
                "generation_prompt_length": len(gen_prompt),
                "context_json_length": len(context_json),
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
            if not final_reply:
                raise ValueError("Empty response from LLM")
        except Exception as e:
            final_reply = f"为你找到了 {total_found} 位从者。"
            await log_trace_event(
                trace_id,
                "generation_output",
                {
                    "reply_length": len(final_reply),
                    "reply_preview": final_reply[:100],
                },
                error=str(e),
            )
        else:
            # ── Trace: generation_output (success) ──
            await log_trace_event(
                trace_id,
                "generation_output",
                {
                    "reply_length": len(final_reply),
                    "reply_preview": final_reply[:100],
                },
            )

    # ── Trace: final ──
    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - request_start) * 1000, 2),
            "total_found": total_found,
            "result": "success" if not result.is_fallback else "fallback",
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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """处理用户对话请求（Skill-Based Architecture）。"""
    user_message = request.message
    trace_id = uuid.uuid4().hex[:8]

    # 确定 skill_calls 来源：preset > params > LLM 路由
    resolved_skill_calls: list[dict] | None = None
    resolved_response_skill = request.response_skill or "respond_servant_list"

    if request.preset_name:
        # 从 Preset Registry 展开为 skill_calls
        preset = PRESET_REGISTRY.get(request.preset_name)
        if preset is None:
            return ChatResponse(
                reply=f"未知的预设名称：{request.preset_name}",
                servants=[],
                count=0,
                query={"error": "unknown_preset", "preset_name": request.preset_name},
                model="error",
                traceId=trace_id,
            )
        resolved_response_skill = preset.response_skill
        resolved_skill_calls = []
        # 用前端 params 覆盖预设模板中的默认参数
        user_params = request.params or {}
        for skill_name in preset.query_skills:
            merged_params = {**preset.param_template.get(skill_name, {})}
            if skill_name in user_params:
                merged_params.update(user_params[skill_name])
            elif user_params and len(preset.query_skills) == 1:
                # 单 Skill 预设：直接用 user_params 作为该 Skill 的参数
                merged_params.update(user_params)
            resolved_skill_calls.append({"skill_name": skill_name, "params": merged_params})

        # B1 策略：用户补充文字走 Stage 2 LLM 路由解析额外 Skills 并合并
        user_text = user_message.strip()
        if user_text:
            try:
                skill_descriptions = [
                    {"name": s.name, "description": s.description}
                    for s in SKILL_REGISTRY.values()
                    if isinstance(s, QuerySkill)
                ]
                routing_prompt = build_routing_prompt(skill_descriptions)
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
                # 合并：预设 Skills + 额外 Skills（同名去重）
                existing_names = {s["skill_name"] for s in resolved_skill_calls}
                for es in extra_skills:
                    if es.get("skill_name") not in existing_names:
                        resolved_skill_calls.append(es)
                        existing_names.add(es["skill_name"])
                # 如果额外路由建议了不同的 response_skill，优先使用
                extra_resp_skill = extra_routing.get("response_skill")
                if extra_resp_skill and extra_resp_skill != "respond_servant_list":
                    resolved_response_skill = extra_resp_skill
                # B1 合并日志将通过 trace event 记录（在 _handle_skill_mode 中）
            except Exception:
                # 补充解析失败不影响预设查询（静默，trace 中可见）
                pass
    elif request.params:
        # 前端直传 skill_calls（params 格式：[{"skill_name": ..., "params": ...}]）
        if isinstance(request.params, list):
            resolved_skill_calls = request.params
        elif isinstance(request.params, dict):
            # 单 dict 视为单个 skill_call
            resolved_skill_calls = [request.params]

    return await _handle_skill_mode(
        user_message=user_message,
        trace_id=trace_id,
        skill_calls=resolved_skill_calls,  # None 则走 LLM 路由
        response_skill_name=resolved_response_skill,
    )


@app.get("/api/chat/stream")
async def chat_stream(message: str, preset_name: str | None = None):
    """SSE 流式对话端点 — 分阶段推送思考过程和结果。

    使用 Skill-Based Architecture：Stage 1 LLM 路由 → SkillExecutor → RAG 生成。
    支持 preset_name 参数：有值时跳过 LLM 路由，直接展开预设 skill_calls。
    """

    async def event_generator():
        trace_id = uuid.uuid4().hex[:8]
        stream_start = time.monotonic()
        model_used = "unknown"

        # ── 阶段 1: Skill 路由（或 Preset 展开） ──
        if preset_name:
            # Preset 模式：跳过 LLM 路由，直接展开预设
            yield _sse_event("thinking", {"phase": "routing", "message": "正在解析预设..."})

            preset = PRESET_REGISTRY.get(preset_name)
            if preset is None:
                yield _sse_event("error", {"phase": "routing", "message": f"未知的预设：{preset_name}"})
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
                    routing_prompt = build_routing_prompt(skill_descriptions)
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
                    existing_names = {s["skill_name"] for s in skill_calls}
                    for es in extra_skills:
                        if es.get("skill_name") not in existing_names:
                            skill_calls.append(es)
                            existing_names.add(es["skill_name"])
                    extra_resp_skill = extra_routing.get("response_skill")
                    if extra_resp_skill and extra_resp_skill != "respond_servant_list":
                        response_skill_name = extra_resp_skill
                    # B1 合并日志将通过后续 trace event 记录
                except Exception:
                    # 补充解析失败不影响预设查询（静默，trace 中可见）
                    pass

            # 推送路由结果
            yield _sse_event(
                "thinking",
                {
                    "phase": "routed",
                    "skill_calls": skill_calls,
                    "response_skill": response_skill_name,
                },
            )

        else:
            # 普通模式：Stage 1 LLM 路由
            yield _sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})

            skill_descriptions = [
                {"name": s.name, "description": s.description}
                for s in SKILL_REGISTRY.values()
                if isinstance(s, QuerySkill)
            ]
            routing_prompt = build_routing_prompt(skill_descriptions)

            # ── Trace: routing_input ──
            await log_trace_event(
                trace_id,
                "routing_input",
                {
                    "query": message,
                    "routing_prompt_length": len(routing_prompt),
                    "skill_count": len(skill_descriptions),
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
                    },
                    error=str(e),
                )
                yield _sse_event("error", {"phase": "routing", "message": "路由失败，请稍后重试"})
                return

            model_used = routing_result.pop("_model", "unknown")
            routing_result.pop("_response_format", None)
            routing_result.pop("_provider", None)
            routing_result.pop("_attempts", None)

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
                },
            )

            # 推送路由结果
            yield _sse_event(
                "thinking",
                {
                    "phase": "routed",
                    "skill_calls": skill_calls,
                    "response_skill": response_skill_name,
                },
            )

            # 检查 fallback
            fallback = routing_result.get("fallback")
            if fallback is not None:
                fb_msg = fallback.get("message", "无法理解你的问题，请尝试更具体的描述。")
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "fallback",
                    },
                )
                yield _sse_event("delta", {"text": fb_msg})
                yield _sse_event("done", {"model": model_used, "traceId": trace_id})
                return

            # 空 skill_calls 且无 fallback
            if not skill_calls:
                no_match_msg = "无法从你的问题中识别出查询条件，请尝试更具体的描述。"
                await log_trace_event(
                    trace_id,
                    "final",
                    {
                        "total_time_ms": (time.monotonic() - stream_start) * 1000,
                        "result": "no_match",
                    },
                )
                yield _sse_event("delta", {"text": no_match_msg})
                yield _sse_event("done", {"model": model_used, "traceId": trace_id})
                return

        # ── 阶段 2: Skill 执行 ──
        yield _sse_event("thinking", {"phase": "executing", "message": "正在检索从者数据..."})

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

        # 执行阶段 fallback（结果为空）
        if result.is_fallback:
            fb_reply = result.fallback_message or "未找到匹配的从者。"
            await log_trace_event(
                trace_id,
                "final",
                {
                    "total_time_ms": (time.monotonic() - stream_start) * 1000,
                    "total_found": 0,
                    "result": "execution_fallback",
                },
            )
            yield _sse_event("delta", {"text": fb_reply})
            yield _sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        # 卡片先行 — 立即推送从者数据
        yield _sse_event(
            "servants",
            {
                "servants": returned_servants,
                "count": len(returned_servants),
                "total": total_found,
            },
        )

        # ── 阶段 3: RAG 生成 ──
        yield _sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

        context_data, _ = _build_context(servants)
        context_data["query_conditions"] = {"skill_calls": skill_calls}
        context_data["applied_filters"] = _describe_filters(skill_calls)
        context_json = json.dumps(context_data, ensure_ascii=False)

        # ── Trace: context_build ──
        await log_trace_event(
            trace_id,
            "context_build",
            {
                "applied_filters": context_data["applied_filters"],
                "top_results_count": len(context_data.get("top_results_details", [])),
                "context_json_length": len(context_json),
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
                "generation_prompt_length": len(gen_prompt),
                "context_json_length": len(context_json),
            },
        )

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
            if not final_reply:
                raise ValueError("Empty response from LLM")
        except Exception as e:
            final_reply = f"为你找到了 {total_found} 位从者。"
            await log_trace_event(
                trace_id,
                "generation_output",
                {
                    "reply_length": len(final_reply),
                    "reply_preview": final_reply[:100],
                },
                error=str(e),
            )
        else:
            # ── Trace: generation_output (success) ──
            await log_trace_event(
                trace_id,
                "generation_output",
                {
                    "reply_length": len(final_reply),
                    "reply_preview": final_reply[:100],
                },
            )

        # 推送生成的文本
        yield _sse_event("delta", {"text": final_reply})

        # ── Trace: final ──
        await log_trace_event(
            trace_id,
            "final",
            {
                "total_time_ms": round((time.monotonic() - stream_start) * 1000, 2),
                "total_found": total_found,
                "result": "success",
            },
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
