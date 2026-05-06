"""
Laplace — FastAPI Server

对话式 FGO 数据查询 API。
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
from pydantic import BaseModel

# 预消化翻译字典（从 config/translations.json 加载，支持热更新）
from server.config_loader import CachedConfig
from server.llm_client import chat_completion
from server.logger import find_trace, log_chat_trace_async, read_traces
from server.prompts import get_generation_prompt, get_system_prompt
from server.query_executor import execute_query, load_database
from server.rate_limiter import RateLimitMiddleware

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
                "totalSelfCharge": s.get("totalSelfCharge"),
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


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str


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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """处理用户对话请求。"""
    user_message = request.message
    trace_id = uuid.uuid4().hex[:8]  # 生成一个 8 位的 trace_id

    # 1. 意图解析 (第一阶段)
    try:
        parsed = await chat_completion(
            system_prompt=get_system_prompt(), user_message=user_message, temperature=0.1, json_mode=True
        )
    except Exception as e:
        print(f"[{trace_id}] LLM Parse Error: {e}")
        await log_chat_trace_async(
            trace_id, user_message, {}, 0, "无法连接到 LLM 或解析失败，请检查模型配置。", error=str(e)
        )
        return ChatResponse(
            reply="抱歉，我遇到了网络问题或模型暂时不可用，请稍后再试。",
            servants=[],
            count=0,
            query={},
            model="error",
            traceId=trace_id,
        )

    model_used = parsed.pop("_model", "unknown")

    # 2. 检查意图
    if parsed.get("intent") != "query_servants":
        reply_text = parsed.get("rawResponse", "抱歉，我目前只能回答 FGO 从者相关的问题。")
        await log_chat_trace_async(trace_id, user_message, parsed, 0, reply_text)
        return ChatResponse(reply=reply_text, servants=[], count=0, query=parsed, model=model_used, traceId=trace_id)

    # 3. 执行查询
    conditions = parsed.get("conditions", {})
    servants = execute_query(conditions)

    # 4. 构建精简 Context（复用共享函数）
    total_found = len(servants)
    context_data, _ = _build_context(servants)
    context_data["query_conditions"] = conditions

    # 5. 第二阶段：生成自然语言回复 (RAG)
    generation_prompt = get_generation_prompt(user_message, json.dumps(context_data, ensure_ascii=False))

    try:
        # LLM 的第二次调用（非严格 JSON，返回纯文本）
        generation_response = await chat_completion(
            system_prompt="You are a helpful AI assistant. You MUST strictly follow the provided data and NEVER use your internal knowledge about FGO.",
            user_message=generation_prompt,
            temperature=0.1,
            json_mode=False,
        )
        final_reply = generation_response.get("text", "").strip()
        if not final_reply:
            raise ValueError("Empty response from LLM")
    except Exception as e:
        print(f"[{trace_id}] RAG Generate Error: {e}")
        # 如果生成失败，降级回旧的逻辑
        response_template = parsed.get("responseTemplate", "为你找到了以下从者：")
        final_reply = f"{response_template}（共 {total_found} 位）"
        if total_found > MAX_CONTEXT_SIZE:
            final_reply += f"（仅显示前 {MAX_CONTEXT_SIZE} 位详情，更多请查看卡片）"

    # 记录完整链路（含 LLM 调用元数据）
    context_data["llm_attempts"] = parsed.get("_attempts", [])
    context_data["llm_model"] = model_used
    await log_chat_trace_async(
        trace_id=trace_id,
        user_message=user_message,
        parsed_intent=conditions,
        found_count=total_found,
        final_reply=final_reply,
        context=context_data,
    )

    # 限制返回给前端的数量，避免响应过大
    returned_servants = servants[:MAX_RESULTS]

    return ChatResponse(
        reply=final_reply,
        servants=returned_servants,
        count=total_found,
        query=conditions,
        model=model_used,
        traceId=trace_id,
    )


@app.get("/api/chat/stream")
async def chat_stream(message: str):
    """SSE 流式对话端点 — 分阶段推送思考过程和结果。"""

    async def event_generator():
        trace_id = uuid.uuid4().hex[:8]
        model_used = "unknown"

        # ── 阶段 1: 意图解析 ──
        yield _sse_event("thinking", {"phase": "parsing", "message": "正在理解你的问题..."})

        try:
            parsed = await chat_completion(
                system_prompt=get_system_prompt(),
                user_message=message,
                temperature=0.1,
                json_mode=True,
            )
        except Exception as e:
            print(f"[{trace_id}] LLM Parse Error: {e}")
            await log_chat_trace_async(trace_id, message, {}, 0, "意图解析失败", error=str(e))
            yield _sse_event("error", {"phase": "parsing", "message": "意图解析失败，请稍后重试"})
            return

        model_used = parsed.pop("_model", "unknown")
        parsed.pop("_response_format", None)

        # 推送解析结果
        yield _sse_event(
            "thinking",
            {
                "phase": "parsed",
                "intent": parsed.get("intent", "unknown"),
                "conditions": parsed.get("conditions", {}),
            },
        )

        # 非查询意图 — 直接返回文本
        if parsed.get("intent") != "query_servants":
            reply_text = parsed.get("rawResponse", "抱歉，我目前只能回答 FGO 从者相关的问题。")
            await log_chat_trace_async(trace_id, message, parsed, 0, reply_text)
            yield _sse_event("delta", {"text": reply_text})
            yield _sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        # ── 阶段 2: 数据检索 ──
        yield _sse_event("thinking", {"phase": "querying", "message": "正在检索从者数据..."})

        conditions = parsed.get("conditions", {})
        servants = execute_query(conditions)
        total_found = len(servants)
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

        # ── 阶段 3: RAG 生成 ──
        yield _sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

        context_data, _ = _build_context(servants)
        context_data["query_conditions"] = conditions
        generation_prompt = get_generation_prompt(message, json.dumps(context_data, ensure_ascii=False))

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
            response_template = parsed.get("responseTemplate", "为你找到了以下从者：")
            final_reply = f"{response_template}（共 {total_found} 位）"
            if total_found > MAX_CONTEXT_SIZE:
                final_reply += f"（仅显示前 {MAX_CONTEXT_SIZE} 位详情，更多请查看卡片）"

        # 推送生成的文本
        yield _sse_event("delta", {"text": final_reply})

        # 记录完整链路（含 LLM 调用元数据）
        context_data["llm_attempts"] = parsed.get("_attempts", [])
        context_data["llm_model"] = model_used
        await log_chat_trace_async(
            trace_id=trace_id,
            user_message=message,
            parsed_intent=conditions,
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
