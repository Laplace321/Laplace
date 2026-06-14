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
from server.logger import bind_trace_id, find_trace, read_trace_summaries, read_traces
from server.pipeline import ChatResponse, handle_skill_mode, resume_skill_mode, stream_chat_events
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
    confirmation_context: str | None = None
    # Task 4 Batch B：多轮对话会话 ID（前端 UUID）。空字符串=单轮模式。
    session_id: str = ""


class ResumeRequest(BaseModel):
    """从 pending checkpoint 恢复执行的请求（Task 4 Batch B）。

    系统在 clarify_node 主动中断时会把 PipelineState 保存到 SessionStore，
    前端拿到带 ``query.pending=True`` 的响应后让用户补充答复，再调 ``/api/chat/resume``。
    """

    session_id: str
    message: str


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
    rating: str | None = None,
    _=Depends(require_admin),
):
    """日志列表（分页 + 关键词搜索 + 评分筛选，需要 Admin 登录）。"""
    return read_trace_summaries(limit=limit, offset=offset, keyword=keyword, rating=rating)


@app.get("/api/admin/logs/stats")
async def admin_logs_stats(days: int = 7, _=Depends(require_admin)):
    """日志统计汇总（PV/UV/路径分布/日期趋势/评分分布 + v0.5.1 BI 维度）。

    在原 ``compute_log_stats`` 输出基础上叠加 ``dimensions`` 字段，
    展示 pipeline / turn_type / skill_name / error_reason 切分。
    """
    from server.bi_index import query_dimension_stats
    from server.logger import compute_log_stats

    base = compute_log_stats(days=days)
    try:
        base["dimensions"] = query_dimension_stats(days=days)
    except Exception:
        # 任何异常不阻塞老 schema 字段返回
        base["dimensions"] = {
            "by_pipeline": [],
            "by_turn_type": [],
            "by_skill": [],
            "by_error_reason": [],
        }
    return base


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
    import asyncio

    from server.bi_index import upsert_turn
    from server.logger import log_trace_event

    await log_trace_event(body.trace_id, "rating", {"rating": body.rating})
    # ADR-032：评分写入 JSONL 后同步刷新 BI 索引（rating 列），确保统计面板可见。
    # upsert_turn 是同步 SQLite 写，使用 to_thread 避免阻塞事件循环。
    try:
        await asyncio.to_thread(upsert_turn, body.trace_id)
    except Exception:  # noqa: BLE001
        # upsert_turn 内部已 try/except，此处仅 belt-and-suspenders
        pass
    return {"ok": True}


class TrackEvent(BaseModel):
    """前端埋点事件。"""

    event: str
    properties: dict = {}
    timestamp: str | None = None
    session_id: str | None = None


@app.post("/api/track")
async def track_event(body: TrackEvent):
    """接收前端埋点事件，写入日志系统。"""
    from server.logger import log_trace_event

    trace_id = body.properties.get("trace_id", "unknown")
    await log_trace_event(
        trace_id,
        f"frontend_{body.event}",
        {
            "event": body.event,
            "properties": body.properties,
            "session_id": body.session_id,
        },
    )
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


def _validate_skill_registry():
    """校验 SKILL_REGISTRY 完整性和每个 Skill 的基本合法性。

    检查项：
    1. 每个已注册 Skill 的 name / description 非空
    2. 有 params_schema 的 Skill，尝试获取 JSON Schema 验证 Pydantic 模型合法性
    3. 对比 server/skills/query/ 目录下的 .py 文件与 SKILL_REGISTRY 中的 QuerySkill，
       检测是否有 Skill 文件存在但未注册（忘记在 __init__.py 中导入）
    校验失败时输出 warning 日志，不阻塞启动。
    """
    from server.skills.base import ResponseSkill

    # ── 检查 1：name / description 非空 ──
    for skill_name, skill in SKILL_REGISTRY.items():
        if not skill.name:
            print(f"⚠️  Skill 注册异常：SKILL_REGISTRY 中存在 name 为空的 Skill（key='{skill_name}'）")
        if not skill.description:
            print(f"⚠️  Skill '{skill_name}' 缺少 description，路由阶段 LLM 无法正确识别此 Skill")

    # ── 检查 2：params_schema 合法性 ──
    for skill_name, skill in SKILL_REGISTRY.items():
        if not isinstance(skill, QuerySkill):
            continue
        schema_cls = skill.params_schema
        if schema_cls is None:
            continue
        try:
            schema_cls.model_json_schema()
        except Exception as schema_err:
            print(f"⚠️  Skill '{skill_name}' 的 params_schema 生成 JSON Schema 失败: {schema_err}")

    # ── 检查 3：文件 vs 注册表一致性 ──
    query_dir = Path(__file__).parent / "skills" / "query"
    if query_dir.exists():
        # 获取目录中所有非 __init__ 的 .py 文件名（不含扩展名）
        file_modules = {f.stem for f in query_dir.glob("*.py") if f.stem != "__init__" and not f.stem.startswith("_")}
        # 获取已注册的 QuerySkill 的模块名（从类的 __module__ 提取最后一段）
        registered_modules = set()
        for skill in SKILL_REGISTRY.values():
            if isinstance(skill, QuerySkill):
                module_name = type(skill).__module__
                # module_name 格式如 "server.skills.query.lookup_servant"
                registered_modules.add(module_name.rsplit(".", 1)[-1])

        unregistered = file_modules - registered_modules
        if unregistered:
            print(f"⚠️  以下 Skill 文件存在于 server/skills/query/ 但未注册到 SKILL_REGISTRY: {sorted(unregistered)}")
            print("   请检查 server/skills/__init__.py 的 _SKILL_MODULES 列表是否遗漏了导入")

    # ── 检查 4：Response Skills 文件 vs 注册表一致性 ──
    response_dir = Path(__file__).parent / "skills" / "response"
    if response_dir.exists():
        response_file_modules = {
            f.stem for f in response_dir.glob("*.py") if f.stem != "__init__" and not f.stem.startswith("_")
        }
        registered_response_modules = set()
        for skill in SKILL_REGISTRY.values():
            if isinstance(skill, ResponseSkill):
                module_name = type(skill).__module__
                registered_response_modules.add(module_name.rsplit(".", 1)[-1])

        unregistered_response = response_file_modules - registered_response_modules
        if unregistered_response:
            print(
                f"⚠️  以下 Response Skill 文件存在于 server/skills/response/ 但未注册: {sorted(unregistered_response)}"
            )

    registered_query_count = sum(1 for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill))
    registered_response_count = sum(1 for s in SKILL_REGISTRY.values() if isinstance(s, ResponseSkill))
    print(
        f"✅  Skill 注册表校验完成：{registered_query_count} 个 QuerySkill + "
        f"{registered_response_count} 个 ResponseSkill = {len(SKILL_REGISTRY)} 个已注册"
    )


@app.on_event("startup")
async def startup():
    """启动时预加载数据库、校验配置一致性、启动监控探活。"""
    load_database()
    _validate_translations()
    _validate_skill_registry()

    # 启动后台模型探活任务
    from server.monitor.health_checker import start_probe_loop

    start_probe_loop()


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, raw_request: Request):
    """处理用户对话请求（Skill-Based Architecture）。"""
    user_message = request.message
    trace_id = uuid.uuid4().hex[:8]
    bind_trace_id(trace_id)
    client_ip = raw_request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        raw_request.client.host if raw_request.client else "unknown"
    )

    # 确定 skill_calls 来源：preset > params > LLM 路由
    resolved_skill_calls: list[dict] | None = None
    resolved_response_skill = request.response_skill or "respond_servant_list"
    resolved_target_pipeline = "A"

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
        client_ip=client_ip,
        target_pipeline=resolved_target_pipeline,
        confirmation_context=request.confirmation_context,
        session_id=request.session_id or "",
    )


@app.post("/api/chat/resume", response_model=ChatResponse)
async def chat_resume(request: ResumeRequest, raw_request: Request):
    """从 pending checkpoint 恢复执行（Task 4 Batch B）。

    场景：``/api/chat`` 返回 ``query.pending=True`` 的 clarification 响应后，
    用户在前端补充答复（如选择某个选项），调本端口续做。
    与 ``confirmation_context`` 机制等价但保留多轮 ``session_id`` 链路。
    """
    if not request.session_id:
        return ChatResponse(
            reply="缺少 session_id，无法恢复对话。",
            servants=[],
            count=0,
            query={"error": "missing_session_id"},
            model="error",
            traceId=None,
        )
    trace_id = uuid.uuid4().hex[:8]
    bind_trace_id(trace_id)
    client_ip = raw_request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        raw_request.client.host if raw_request.client else "unknown"
    )
    return await resume_skill_mode(
        session_id=request.session_id,
        supplement_message=request.message or "",
        trace_id=trace_id,
        client_ip=client_ip,
    )


@app.get("/api/chat/stream")
async def chat_stream(
    request: Request,
    message: str,
    preset_name: str | None = None,
    confirmation_context: str | None = None,
    confirmation_id: str | None = None,
    session_id: str = "",
):
    """SSE 流式对话端点 — 分阶段推送思考过程和结果。

    使用 Skill-Based Architecture：Stage 1 LLM 路由 → SkillExecutor → RAG 生成。
    支持 preset_name 参数：有值时跳过 LLM 路由，直接展开预设 skill_calls。
    支持 confirmation_context 参数：用户确认选择后携带的上下文，用于精确路由。
    支持 confirmation_id 参数：用户选择的选项 ID（collectionNo），用于精确定位实体。
    支持 session_id 参数：前端会话 ID，用于多轮对话状态关联，由 stream_chat_events
    透传到 SessionStore 完成多轮闭环。
    """
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.client.host
        if request.client
        else "unknown"
    )

    async def event_generator():
        async for event in stream_chat_events(
            message,
            preset_name,
            client_ip=client_ip,
            confirmation_context=confirmation_context,
            confirmation_id=confirmation_id,
            session_id=session_id,
        ):
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
    """健康检查（含模型可用性状态）。"""
    from server.monitor.metrics import get_collector

    model_status = get_collector().get_model_status()
    all_ok = all(model_status.values()) if model_status else True
    return {
        "status": "ok" if all_ok else "degraded",
        "service": "laplace",
        "models": model_status,
    }


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus text exposition format 端点。"""
    from fastapi.responses import Response

    from server.monitor.metrics import get_collector

    body = get_collector().to_prometheus_text()
    return Response(content=body, media_type="text/plain; charset=utf-8")


@app.get("/api/admin/monitor")
async def admin_monitor(minutes: int = 5, _=Depends(require_admin)):
    """运维监控仪表盘 API — 返回指定时间窗口的汇总指标 + 告警历史。"""
    from server.monitor.metrics import get_collector

    collector = get_collector()
    summary = collector.get_summary(minutes=minutes)
    summary["alert_history"] = collector.get_alert_history()
    return summary


# 挂载 Admin 后台静态文件（必须在 "/" 之前注册，否则会被 catch-all 拦截）
_admin_dir = Path(__file__).parent.parent / "admin"
if _admin_dir.exists():
    app.mount("/admin", StaticFiles(directory=str(_admin_dir), html=True), name="admin-static")

# 挂载前端静态文件目录
app.mount("/", StaticFiles(directory="demo", html=True), name="static")
