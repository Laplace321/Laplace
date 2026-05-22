"""
Laplace — FastAPI Server

对话式 FGO 数据查询 API。
支持传统 JSON 端点和 SSE 流式端点。
"""

import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

# ── 加载 .env（本地开发用，Docker 由 entrypoint 处理）──
# 必须在 server.* import 之前调用，因为子模块加载时可能读取环境变量
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Admin 模块 ──
from server.admin.auth import require_admin
from server.admin.auth import router as auth_router
from server.admin.routes import router as admin_routes_router

# ── 业务模块 ──
from server.llm import chat_completion
from server.logger import find_trace, read_trace_summaries, read_traces
from server.pipeline import ChatResponse, handle_skill_mode, stream_event_generator
from server.prompts import build_routing_prompt
from server.query_executor import load_database
from server.rate_limiter import RateLimitMiddleware
from server.schemas import parse_routing_response, routing_response_json_schema
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.skills.presets import PRESET_REGISTRY
from server.translation import (
    get_class_map as _get_class_map,
)

app = FastAPI(
    title="Laplace API",
    description="AI Native FGO 数据助手",
    version="0.2.0",
)

# ── Admin 路由挂载 ──
app.include_router(auth_router, prefix="/api/admin")
app.include_router(admin_routes_router, prefix="/api/admin")

# ── 从者头像代理（开发环境替代 Nginx 的 /faces/ 静态服务）──
from server.face_proxy import router as face_proxy_router

app.include_router(face_proxy_router)

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


@app.get("/api/traces")
async def list_traces(request: Request, limit: int = 20, _=Depends(require_admin)):
    """返回最近 N 条 trace（需要 Admin 登录）。"""
    traces = read_traces(limit=limit)
    return traces


@app.get("/api/traces/{trace_id}")
async def get_trace(request: Request, trace_id: str, _=Depends(require_admin)):
    """按 trace_id 查询单条 trace 详情（需要 Admin 登录）。"""
    trace = find_trace(trace_id)
    if trace is None:
        return JSONResponse(status_code=404, content={"error": f"trace {trace_id} 未找到"})
    return trace


# ── Admin: 日志查看 API ──


@app.get("/api/admin/logs")
async def admin_list_logs(
    limit: int = 50,
    offset: int = 0,
    keyword: str | None = None,
    _=Depends(require_admin),
):
    """日志列表（分页 + 关键词搜索，需要 Admin 登录）。"""
    return read_trace_summaries(limit=limit, offset=offset, keyword=keyword)


@app.get("/api/admin/logs/stats")
async def admin_logs_stats(days: int = 7, _=Depends(require_admin)):
    """日志统计汇总（PV/UV/路径分布/日期趋势/评分分布）。"""
    from server.logger import compute_log_stats

    return compute_log_stats(days=days)


@app.get("/api/admin/logs/{trace_id}")
async def admin_get_log(trace_id: str, _=Depends(require_admin)):
    """单条 trace 的完整多阶段详情（需要 Admin 登录）。"""
    trace = find_trace(trace_id)
    if trace is None:
        return JSONResponse(status_code=404, content={"error": f"trace {trace_id} 未找到"})
    return trace


# ── 评分 API（无需鉴权，用户侧操作）──


class RateRequest(BaseModel):
    """用户评分请求。"""

    trace_id: str
    rating: str  # "bad" | "ok" | "good"


@app.post("/api/rate")
async def rate_response(body: RateRequest):
    """记录用户对 AI 回复的评分（糟糕/一般/优秀）。"""
    if body.rating not in ("bad", "ok", "good"):
        return JSONResponse(status_code=400, content={"error": "rating 必须为 bad/ok/good"})
    from server.logger import log_trace_event

    await log_trace_event(body.trace_id, "rating", {"rating": body.rating})
    return {"ok": True}


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
                existing_map = {s["skill_name"]: s for s in resolved_skill_calls}
                for es in extra_skills:
                    es_name = es.get("skill_name")
                    if es_name in existing_map:
                        # 同名 Skill：LLM 解析的参数补充 preset 缺失的字段
                        for k, v in es.get("params", {}).items():
                            if k not in existing_map[es_name]["params"]:
                                existing_map[es_name]["params"][k] = v
                    else:
                        resolved_skill_calls.append(es)
                        existing_map[es_name] = es
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

    return await handle_skill_mode(
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
        async for event in stream_event_generator(message, preset_name):
            yield event

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


# 挂载 Admin 后台静态文件（必须在 "/" 之前注册，否则会被 catch-all 拦截）
_admin_dir = Path(__file__).parent.parent / "admin"
if _admin_dir.exists():
    app.mount("/admin", StaticFiles(directory=str(_admin_dir), html=True), name="admin-static")

# 挂载前端静态文件目录
app.mount("/", StaticFiles(directory="demo", html=True), name="static")
