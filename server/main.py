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

from server.agent.agent_loop import AgentResult, agent_route
from server.agent.tool_handlers import TOOL_HANDLERS

# 预消化翻译字典（从 config/translations.json 加载，支持热更新）
from server.config_loader import CachedConfig
from server.llm_client import chat_completion
from server.logger import find_trace, log_trace_event, read_trace_summaries, read_traces
from server.prompts import build_routing_prompt, get_generation_prompt
from server.query_executor import load_database
from server.rate_limiter import RateLimitMiddleware
from server.schemas import parse_routing_response, routing_response_json_schema
from server.skills.base import SKILL_REGISTRY, QuerySkill
from server.skills.executor import SkillExecutor
from server.skills.presets import PRESET_REGISTRY, Preset

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
                from server.data_loader import merge_effect_overlay

                effects = merge_effect_overlay(data.get("effects", []))
                for effect in effects:
                    name = effect.get("name")
                    aliases = effect.get("aliases_zh", [])
                    if name and aliases:
                        _effect_map[name] = aliases[0]
    return _effect_map.get(effect_code, effect_code)


# === 路由模式切换 ===
# === 共享业务逻辑 ===

MAX_CONTEXT_SIZE = 5
MAX_RESULTS = 50


def _effect_qualifier(params: dict) -> str:
    """根据效果参数生成中文前缀修饰语（目标类型+数值条件）。"""
    parts: list[str] = []
    target_type = params.get("targetType") or params.get("target_type")
    if target_type == "party":
        parts.append("给队友的")
    elif target_type == "self":
        parts.append("自身的")
    elif target_type == "enemy":
        parts.append("对敌方的")

    min_val = params.get("minValue") or params.get("min_value")
    max_val = params.get("maxValue") or params.get("max_value")
    if min_val is not None and max_val is not None:
        parts.append(f"{min_val}%~{max_val}%")
    elif min_val is not None:
        parts.append(f"≥{min_val}%")
    elif max_val is not None:
        parts.append(f"≤{max_val}%")

    return "".join(parts)


def _describe_filters(skill_calls: list[dict]) -> list[str]:
    """将 skill_calls 转换为人类可读的筛选条件描述列表。

    例如: ["技能效果包含「Arts提升」", "稀有度 = 5星"]
    供 LLM 在生成回复时明确知道系统做了什么筛选。
    """
    descriptions: list[str] = []
    for call in skill_calls:
        name = call.get("skill_name", "")
        params = call.get("params", {})
        if name == "search_by_effect":
            effect = params.get("effect", "")
            translated = get_effect_translation(effect) if effect else effect
            source = params.get("source", "both")
            qualifier = _effect_qualifier(params)
            if source == "skill":
                descriptions.append(f"技能效果包含「{qualifier}{translated}」")
            elif source == "np":
                descriptions.append(f"宝具效果包含「{qualifier}{translated}」")
            else:
                descriptions.append(f"效果包含「{qualifier}{translated}」（技能或宝具）")
        elif name == "search_by_skill_effect":
            effect = params.get("skillEffect") or params.get("effect", "")
            translated = get_effect_translation(effect) if effect else effect
            qualifier = _effect_qualifier(params)
            descriptions.append(f"技能效果包含「{qualifier}{translated}」")
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
            parts = []
            card = params.get("cardType") or params.get("cards")
            np_card = params.get("npCard") or params.get("np_card")
            np_target = params.get("npTarget") or params.get("np_target")
            if card:
                parts.append(f"配卡包含「{card}」")
            if np_card:
                np_card_map_local = _get_np_card_map()
                parts.append(f"宝具卡色 = {np_card_map_local.get(np_card.lower(), np_card)}")
            if np_target:
                target_map = {"all": "全体(光炮)", "one": "单体", "support": "辅助"}
                parts.append(f"宝具目标 = {target_map.get(np_target.lower(), np_target)}")
            descriptions.append(" + ".join(parts) if parts else "配卡筛选")
        elif name == "search_by_traits":
            trait = params.get("trait", "")
            descriptions.append(f"特性包含「{trait}」")
        elif name == "search_by_attribute":
            attr = params.get("attribute", "")
            descriptions.append(f"属性 = {attr}")
        elif name == "compare_servants":
            names = params.get("names", [])
            descriptions.append(f"对比从者「{'」与「'.join(names)}」")
        elif name == "lookup_servant":
            query = params.get("name") or params.get("query", "")
            descriptions.append(f"查询从者「{query}」")
        else:
            # 兜底：仅输出 Skill 中文 description，禁止暴露参数结构
            from server.skills.base import SKILL_REGISTRY

            skill_instance = SKILL_REGISTRY.get(name)
            if skill_instance:
                descriptions.append(skill_instance.description)
            else:
                descriptions.append(f"筛选条件: {name}")
    return descriptions


def _describe_agent_filters(tool_trace: list[dict]) -> list[str]:
    """从 Agent tool_trace 提取筛选条件的中文描述。

    Agent 的 tool_trace 格式: [{"tool": "search_servants", "args": {...}, ...}]
    将 search_servants 的 args 映射为人类可读的中文。
    """
    descriptions: list[str] = []
    class_map = _get_class_map()
    np_card_map = _get_np_card_map()
    for step in tool_trace:
        tool_name = step.get("tool", "")
        args = step.get("args", {})
        if tool_name == "search_servants":
            if "class_name" in args:
                cn = class_map.get(str(args["class_name"]).lower(), args["class_name"])
                descriptions.append(f"职阶 = {cn}")
            if "rarity" in args:
                op = args.get("rarity_op", "eq")
                op_map = {"eq": "=", "gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
                descriptions.append(f"稀有度 {op_map.get(op, op)} {args['rarity']}星")
            if "effects" in args:
                for eff in args["effects"]:
                    translated = get_effect_translation(eff) if eff else eff
                    descriptions.append(f"效果包含「{translated}」")
            if "np_card" in args:
                card = np_card_map.get(str(args["np_card"]).lower(), args["np_card"])
                descriptions.append(f"宝具卡色 = {card}")
            if "np_target" in args:
                target_map = {"all": "全体(光炮)", "one": "单体", "support": "辅助"}
                descriptions.append(f"宝具目标 = {target_map.get(args['np_target'], args['np_target'])}")
            if "np_charge_value" in args:
                op = args.get("np_charge_op", "gte")
                op_map = {"eq": "=", "gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
                descriptions.append(f"NP充能 {op_map.get(op, op)} {args['np_charge_value']}%")
            if "trait_names" in args:
                for t in args["trait_names"]:
                    descriptions.append(f"特性包含「{t}」")
            if "attribute" in args:
                descriptions.append(f"属性 = {args['attribute']}")
        elif tool_name == "lookup_servant":
            name = args.get("name", "")
            descriptions.append(f"查询从者「{name}」")
        elif tool_name == "compare_servants":
            names = args.get("names", [])
            descriptions.append(f"对比从者「{'」与「'.join(names)}」")
    return descriptions


def _build_context(servants: list[dict]) -> tuple[dict, list[dict]]:
    """构建预消化的精简 Context 供 RAG 生成使用。

    Returns:
        (context_data, top_results) — context_data 含 total_found 等元信息；
        top_results 为翻译后的前 N 条详情。
    """
    total_found = len(servants)
    top_results = []

    # 翻译映射缓存
    class_map = _get_class_map()
    np_card_map = _get_np_card_map()
    np_target_map = _get_np_target_map()

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
                "名称": s.get("name"),
                "中文名": s.get("aliasCN"),
                "职阶": class_map.get(str(raw_class_name).lower(), raw_class_name),
                "稀有度": s.get("rarity"),
                "配卡": s.get("cards"),
                "总充能": s.get("totalCharge"),
                "宝具卡色": np_card_map.get(str(raw_np_card).lower(), raw_np_card),
                "宝具目标": np_target_map.get(str(raw_np_target).lower(), raw_np_target),
                "技能效果": translated_effects,
                "宝具效果": translated_np_effects,
            }
        )

    # 全局统计摘要（基于全部从者，而非仅 top N）
    from collections import Counter

    stats_summary = {}
    if total_found > MAX_CONTEXT_SIZE:
        np_card_dist = Counter(np_card_map.get(str(s.get("npCard", "")).lower(), s.get("npCard")) for s in servants)
        class_dist = Counter(class_map.get(str(s.get("className", "")).lower(), s.get("className")) for s in servants)
        rarity_dist = Counter(s.get("rarity") for s in servants)
        stats_summary = {
            "宝具卡色分布": dict(np_card_dist),
            "职阶分布": dict(class_dist),
            "稀有度分布": {f"{k}星": v for k, v in sorted(rarity_dist.items(), reverse=True)},
        }

    return {
        "匹配总数": total_found,
        "筛选条件": {},  # 由调用方填充
        "全局统计": stats_summary,
        "代表从者详情": top_results,
    }, top_results


# === Fallback 分类与模板 ===

_FALLBACK_TEMPLATES = {
    "GREETING": (
        "你好！我是 **Laplace**，一个 FGO 智能数据助手。"
        "你可以用日常语言向我提问，我会从数据库中检索和分析从者信息。\n\n"
        "**我能帮你做这些事：**\n"
        "- **条件筛选** — 按职阶、星级、配卡、属性、特性等条件筛选从者\n"
        "- **效果搜索** — 搜索拥有特定效果的从者（如充能、增伤、无敌、闪避等）\n"
        "- **从者详情** — 查看某个从者的完整数据和技能信息\n"
        "- **从者对比** — 把几个从者放在一起比较，分析各自优劣\n\n"
        "**试试这样问我：**\n"
        '- "30自充以上的五星Caster"\n'
        '- "有增伤技能的从者"\n'
        '- "对比梅林和斯卡蒂"\n'
        '- "查一下村正"'
    ),
    "OUT_OF_SCOPE": (
        "抱歉，这个问题超出了我的能力范围。"
        "我是一个 FGO 从者数据助手，只能帮你查询和分析从者信息。\n\n"
        "你可以试试问我：\n"
        '- "有哪些5星弓阶从者"\n'
        '- "对比梅林和斯卡蒂"\n'
        '- "有无敌技能的从者"'
    ),
    "UNSUPPORTED": (
        "这个功能暂时还不支持。目前我只能帮你查询从者数据（筛选、搜索、对比），"
        "还不能做队伍搭配推荐、关卡攻略、礼装推荐等。\n\n"
        "你可以试试问我从者相关的查询，比如：\n"
        '- "50%以上充能的五星Caster"\n'
        '- "有增伤效果的从者"'
    ),
}


def _classify_agent_reply(reply: str) -> tuple[str | None, str]:
    """解析 Agent 回复中的分类标记，返回 (category, clean_reply)。

    Agent Prompt 要求在无需调用工具时，以 [GREETING]/[OUT_OF_SCOPE]/[UNSUPPORTED] 开头。
    检测到标记后替换为标准化模板回复。
    """
    for tag in ("GREETING", "OUT_OF_SCOPE", "UNSUPPORTED"):
        if reply.strip().startswith(f"[{tag}]"):
            return tag, _FALLBACK_TEMPLATES[tag]
    return None, reply


# === 统一后置管线 ===


async def _generate_reply(
    user_message: str,
    servants: list[dict],
    applied_filters: list[str],
    trace_id: str,
    response_skill=None,
    fallback_reply: str | None = None,
) -> tuple[str, str]:
    """统一的 Generation Prompt 调用。Agent/Preset 共用。

    Args:
        user_message: 用户原始消息
        servants: 从者数据列表
        applied_filters: 筛选条件中文描述
        trace_id: 日志追踪 ID
        response_skill: 可选的 ResponseSkill 实例
        fallback_reply: Generation 失败时的降级回复

    Returns:
        (final_reply, result_status) — 最终回复和状态标记
    """
    context_data, _ = _build_context(servants)
    context_data["已应用的筛选条件"] = applied_filters
    context_data["筛选条件"] = applied_filters
    context_json = json.dumps(context_data, ensure_ascii=False)

    if response_skill is not None and hasattr(response_skill, "build_prompt"):
        gen_prompt = response_skill.build_prompt(user_message, context_json)
    else:
        gen_prompt = get_generation_prompt(user_message, context_json)

    # ── Trace: context_build ──
    await log_trace_event(
        trace_id,
        "context_build",
        {
            "applied_filters": applied_filters,
            "context_data": context_data,
        },
    )

    # ── Trace: generation_input ──
    await log_trace_event(
        trace_id,
        "generation_input",
        {"generation_prompt": gen_prompt},
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
        final_reply = fallback_reply or f"为你找到了 {len(servants)} 位从者。"
        await log_trace_event(
            trace_id,
            "generation_output",
            {"reply": final_reply, "source": "fallback"},
            error=str(e),
        )
        return final_reply, "generation_fallback"
    else:
        await log_trace_event(
            trace_id,
            "generation_output",
            {"reply": final_reply, "source": "generation"},
        )
        return final_reply, "success"


async def _log_final(trace_id: str, total_time_ms: float, result: str, **extra_fields):
    """统一的 final trace 写入。"""
    data = {"total_time_ms": round(total_time_ms, 2), "result": result}
    data.update(extra_fields)
    await log_trace_event(trace_id, "final", data)


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


# ── Admin: 日志查看 API ──


@app.get("/api/admin/logs")
async def admin_list_logs(
    limit: int = 50,
    offset: int = 0,
    keyword: str | None = None,
):
    """日志列表（分页 + 关键词搜索）。"""
    return read_trace_summaries(limit=limit, offset=offset, keyword=keyword)


@app.get("/api/admin/logs/{trace_id}")
async def admin_get_log(trace_id: str):
    """单条 trace 的完整多阶段详情。"""
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


async def _handle_chat(
    user_message: str,
    trace_id: str,
) -> ChatResponse:
    """Agent 路由模式的核心处理逻辑（唯一路由入口）。

    LLM 自主决定调用哪些工具，支持多轮 tool 调用和自我纠正。
    Fallback 场景通过 Agent Prompt 的分类标记自动识别。
    """
    # ── Trace: routing_input ──
    await log_trace_event(
        trace_id,
        "routing_input",
        {
            "query": user_message,
            "source": "agent",
        },
    )

    try:
        agent_result: AgentResult = await agent_route(
            user_message=user_message,
            tool_handlers=TOOL_HANDLERS,
            trace_id=trace_id,
            max_rounds=5,
        )
    except Exception as e:
        await _log_final(trace_id, total_time_ms=0, result="agent_error")
        return ChatResponse(
            reply="抱歉，处理你的问题时遇到了问题，请稍后重试。",
            servants=[],
            count=0,
            query={"mode": "agent", "error": str(e)},
            model="error",
            traceId=trace_id,
        )

    # ── Trace: agent_complete ──
    await log_trace_event(
        trace_id,
        "agent_complete",
        {
            "rounds": agent_result.rounds,
            "total_tokens": agent_result.total_tokens,
            "elapsed_ms": round(agent_result.elapsed_ms, 2),
            "tool_trace": agent_result.tool_trace,
            "is_fallback": agent_result.is_fallback,
        },
    )

    returned_servants = agent_result.servants_data or []
    model_used = f"agent_{agent_result.rounds}r"
    agent_elapsed_ms = round(agent_result.elapsed_ms, 2)
    gen_elapsed_ms = 0.0
    final_result = "fallback" if agent_result.is_fallback else "success"

    # ── Agent 后置管线：有从者数据时走统一 Generation Prompt ──
    if returned_servants and not agent_result.is_fallback:
        gen_start = time.monotonic()
        applied_filters = _describe_agent_filters(agent_result.tool_trace)

        final_reply, gen_status = await _generate_reply(
            user_message=user_message,
            servants=returned_servants,
            applied_filters=applied_filters,
            trace_id=trace_id,
            fallback_reply=agent_result.reply,
        )
        final_result = gen_status
        gen_elapsed_ms = round((time.monotonic() - gen_start) * 1000, 2)
    else:
        # 兜底：无从者数据或 fallback 时，走分类逻辑
        category, final_reply = _classify_agent_reply(agent_result.reply)
        if category:
            final_result = f"fallback_{category.lower()}"
        else:
            final_result = "fallback" if agent_result.is_fallback else "no_servants"

    # ── Trace: final ──
    await _log_final(
        trace_id,
        total_time_ms=agent_elapsed_ms + gen_elapsed_ms,
        result=final_result,
        agent_time_ms=agent_elapsed_ms,
        generation_time_ms=gen_elapsed_ms,
        agent_rounds=agent_result.rounds,
    )

    return ChatResponse(
        reply=final_reply,
        servants=returned_servants,
        count=len(returned_servants),
        query={
            "mode": "agent",
            "rounds": agent_result.rounds,
            "tool_trace": [
                {
                    "round": step["round"],
                    "tool": step["tool"],
                    "result_summary": step.get("result_summary", ""),
                }
                for step in agent_result.tool_trace
            ],
        },
        model=model_used,
        traceId=trace_id,
    )


async def _handle_preset_mode(
    user_message: str,
    trace_id: str,
    skill_calls: list[dict],
    response_skill_name: str = "respond_servant_list",
) -> ChatResponse:
    """Preset 旁路模式：跳过 Agent Loop，直接执行 SkillExecutor + 统一后置管线。

    Preset 由前端预设按钮触发，skill_calls 已确定，无需 LLM 路由。
    """
    request_start = time.monotonic()
    model_used = "preset_mode"

    # ── Trace: routing_input (preset) ──
    await log_trace_event(
        trace_id,
        "routing_input",
        {
            "query": user_message,
            "source": "preset",
            "skill_count": len(skill_calls),
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
        },
    )

    # 执行 Skills
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

    # Fallback 处理
    if result.is_fallback:
        final_reply = result.fallback_message or "未找到匹配的从者。"
        final_result = "execution_fallback"
    else:
        # 统一后置管线：Generation Prompt
        applied_filters = _describe_filters(skill_calls)
        final_reply, final_result = await _generate_reply(
            user_message=user_message,
            servants=servants,
            applied_filters=applied_filters,
            trace_id=trace_id,
            response_skill=result.response_skill,
        )

    # ── Trace: final ──
    await _log_final(
        trace_id,
        total_time_ms=(time.monotonic() - request_start) * 1000,
        result=final_result,
        total_found=total_found,
    )

    return ChatResponse(
        reply=final_reply,
        servants=returned_servants,
        count=total_found,
        query={"mode": "preset", "skill_calls": skill_calls},
        model=model_used,
        traceId=trace_id,
    )


def _expand_preset(preset: Preset, user_params: dict | list | None) -> list[dict]:
    """将 Preset 展开为 skill_calls 列表，合并用户参数。"""
    skill_calls = []
    params = user_params or {}
    for skill_name in preset.query_skills:
        merged_params = {**preset.param_template.get(skill_name, {})}
        if skill_name in params:
            merged_params.update(params[skill_name])
        elif params and len(preset.query_skills) == 1:
            merged_params.update(params)
        skill_calls.append({"skill_name": skill_name, "params": merged_params})
    return skill_calls


async def _merge_b1_extra_skills(
    user_text: str,
    skill_calls: list[dict],
    response_skill_name: str,
) -> tuple[list[dict], str]:
    """B1 策略：用户补充文字走 LLM 路由解析额外 Skills 并合并到 preset 的 skill_calls 中。

    Returns:
        (merged_skill_calls, resolved_response_skill)
    """
    if not user_text.strip():
        return skill_calls, response_skill_name

    try:
        skill_descriptions = [
            {"name": s.name, "description": s.description} for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill)
        ]
        routing_prompt = build_routing_prompt(skill_descriptions)
        extra_routing = await chat_completion(
            system_prompt=routing_prompt,
            user_message=user_text.strip(),
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
                for k, v in es.get("params", {}).items():
                    if k not in existing_map[es_name]["params"]:
                        existing_map[es_name]["params"][k] = v
            else:
                skill_calls.append(es)
                existing_map[es_name] = es
        extra_resp_skill = extra_routing.get("response_skill")
        if extra_resp_skill and extra_resp_skill != "respond_servant_list":
            response_skill_name = extra_resp_skill
    except Exception:
        # 补充解析失败不影响预设查询（静默）
        pass

    return skill_calls, response_skill_name


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """处理用户对话请求。

    路由策略：
    - preset_name → Preset 旁路（SkillExecutor + Generation）
    - 其余 → Agent 路由（唯一路由入口）
    """
    user_message = request.message
    trace_id = uuid.uuid4().hex[:8]

    # Preset 模式
    if request.preset_name:
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
        skill_calls = _expand_preset(preset, request.params)
        response_skill_name = preset.response_skill

        # B1 策略：用户补充文字走 LLM 路由合并
        skill_calls, response_skill_name = await _merge_b1_extra_skills(
            user_message,
            skill_calls,
            response_skill_name,
        )

        return await _handle_preset_mode(
            user_message=user_message,
            trace_id=trace_id,
            skill_calls=skill_calls,
            response_skill_name=response_skill_name,
        )

    # Agent 模式（唯一路由入口）
    return await _handle_chat(
        user_message=user_message,
        trace_id=trace_id,
    )


@app.get("/api/chat/stream")
async def chat_stream(message: str, preset_name: str | None = None):
    """SSE 流式对话端点 — 分阶段推送思考过程和结果。

    路由策略：
    - preset_name → Preset 旁路（SkillExecutor + Generation）
    - 其余 → Agent 路由（唯一路由入口）
    """

    # Agent Tool Labels（中文化，禁止暴露工具名）
    _AGENT_TOOL_LABELS = {
        "search_servants": "正在检索从者数据...",
        "lookup_servant": "正在查询从者详情...",
        "compare_servants": "正在对比从者...",
        "list_effects": "正在查询效果列表...",
        "list_traits": "正在查询特性列表...",
        "list_classes": "正在查询职阶列表...",
        "lookup_skill_detail": "正在查询技能详情...",
    }

    async def event_generator():
        trace_id = uuid.uuid4().hex[:8]
        stream_start = time.monotonic()
        model_used = "unknown"

        # ── Preset 模式：跳过 Agent Loop，直接 SkillExecutor + Generation ──
        if preset_name:
            yield _sse_event("thinking", {"phase": "routing", "message": "正在解析预设..."})

            preset = PRESET_REGISTRY.get(preset_name)
            if preset is None:
                yield _sse_event("error", {"phase": "routing", "message": f"未知的预设：{preset_name}"})
                return

            skill_calls = _expand_preset(preset, None)
            response_skill_name = preset.response_skill
            model_used = "preset_mode"

            # B1 策略：用户补充文字走 LLM 路由合并
            skill_calls, response_skill_name = await _merge_b1_extra_skills(
                message,
                skill_calls,
                response_skill_name,
            )

            # ── Trace: routing ──
            await log_trace_event(
                trace_id,
                "routing_input",
                {"query": message, "source": "preset", "preset_name": preset_name, "skill_count": len(skill_calls)},
            )
            await log_trace_event(
                trace_id,
                "routing_output",
                {
                    "skill_calls": skill_calls,
                    "response_skill": response_skill_name,
                    "fallback": None,
                    "model": model_used,
                },
            )

            yield _sse_event(
                "thinking",
                {"phase": "routed", "message": "意图识别完成", "detail": "、".join(_describe_filters(skill_calls))},
            )

            # ── Skill 执行 ──
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

            if result.is_fallback:
                fb_reply = result.fallback_message or "未找到匹配的从者。"
                await _log_final(
                    trace_id, (time.monotonic() - stream_start) * 1000, "execution_fallback", total_found=0
                )
                yield _sse_event("delta", {"text": fb_reply})
                yield _sse_event("done", {"model": model_used, "traceId": trace_id})
                return

            # 卡片先行
            yield _sse_event(
                "servants", {"servants": returned_servants, "count": len(returned_servants), "total": total_found}
            )

            # ── Generation（统一后置管线）──
            yield _sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

            applied_filters = _describe_filters(skill_calls)
            final_reply, final_result = await _generate_reply(
                user_message=message,
                servants=servants,
                applied_filters=applied_filters,
                trace_id=trace_id,
                response_skill=result.response_skill,
            )

            try:
                yield _sse_event("delta", {"text": final_reply})
            except Exception:
                final_result = "client_disconnected"

            await _log_final(trace_id, (time.monotonic() - stream_start) * 1000, final_result, total_found=total_found)
            yield _sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        # ── Agent 模式（唯一路由入口） ──
        yield _sse_event("thinking", {"phase": "routing", "message": "正在理解你的问题..."})

        # ── Trace: routing_input ──
        await log_trace_event(
            trace_id,
            "routing_input",
            {"query": message, "source": "agent"},
        )

        try:
            agent_result: AgentResult = await agent_route(
                user_message=message,
                tool_handlers=TOOL_HANDLERS,
                trace_id=trace_id,
                max_rounds=5,
            )
        except Exception:
            await _log_final(trace_id, (time.monotonic() - stream_start) * 1000, "agent_error")
            yield _sse_event("error", {"phase": "routing", "message": "处理失败，请稍后重试"})
            return

        model_used = f"agent_{agent_result.rounds}r"
        agent_elapsed_ms = round(agent_result.elapsed_ms, 2)

        # 推送每轮 tool 调用作为 thinking steps
        for step in agent_result.tool_trace:
            label = _AGENT_TOOL_LABELS.get(step["tool"], "正在处理...")
            yield _sse_event(
                "thinking",
                {"phase": "tool_call", "message": label, "detail": step.get("result_summary", "")},
            )

        # ── Trace: agent_complete ──
        await log_trace_event(
            trace_id,
            "agent_complete",
            {
                "rounds": agent_result.rounds,
                "total_tokens": agent_result.total_tokens,
                "elapsed_ms": agent_elapsed_ms,
                "tool_trace": agent_result.tool_trace,
                "is_fallback": agent_result.is_fallback,
            },
        )

        # ── Agent 后置管线：有从者数据时走统一 Generation Prompt ──
        if agent_result.servants_data and not agent_result.is_fallback:
            servants = agent_result.servants_data
            gen_start = time.monotonic()

            applied_filters = _describe_agent_filters(agent_result.tool_trace)
            yield _sse_event(
                "thinking",
                {"phase": "routed", "message": "意图识别完成", "detail": "、".join(applied_filters)},
            )

            # 卡片先行
            yield _sse_event("servants", {"servants": servants, "count": len(servants), "total": len(servants)})

            yield _sse_event("thinking", {"phase": "generating", "message": "正在生成分析..."})

            final_reply, final_result = await _generate_reply(
                user_message=message,
                servants=servants,
                applied_filters=applied_filters,
                trace_id=trace_id,
                fallback_reply=agent_result.reply,
            )

            gen_elapsed_ms = round((time.monotonic() - gen_start) * 1000, 2)

            await _log_final(
                trace_id,
                total_time_ms=(time.monotonic() - stream_start) * 1000,
                result=final_result,
                agent_time_ms=agent_elapsed_ms,
                generation_time_ms=gen_elapsed_ms,
                agent_rounds=agent_result.rounds,
            )

            yield _sse_event("delta", {"text": final_reply})
            yield _sse_event("done", {"model": model_used, "traceId": trace_id})
            return

        # ── Agent 兜底：无从者数据或 fallback，走分类逻辑 ──
        category, final_reply = _classify_agent_reply(agent_result.reply)
        final_result = (
            f"fallback_{category.lower()}" if category else ("fallback" if agent_result.is_fallback else "no_servants")
        )

        await _log_final(
            trace_id,
            total_time_ms=(time.monotonic() - stream_start) * 1000,
            result=final_result,
            agent_time_ms=agent_elapsed_ms,
            agent_rounds=agent_result.rounds,
        )

        yield _sse_event("delta", {"text": final_reply})
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
