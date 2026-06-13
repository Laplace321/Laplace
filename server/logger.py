"""
Laplace — 结构化 Trace 日志

多阶段事件流模式：log_trace_event — 同一 traceId 下按阶段记录事件。

Phase 命名统一在 ``Phase`` 字符串常量类，所有节点和 pipeline.py 必须引用常量
而非裸字符串字面量；新增 phase 时同步加入 ``PHASES`` 集合，便于通过
``validate_phase()`` 在测试中检查未声明的 phase。

trace_id 通过 ``current_trace_id`` ContextVar 在协程间自动传播，节点和
pipeline.py 调用 log_trace_event 时可以省略 trace_id 参数。入口处必须
显式调用 ``bind_trace_id(trace_id)`` 注入当前请求的 trace_id。
"""

import asyncio
import json
import os
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 北京时间 UTC+8
_BEIJING_TZ = timezone(timedelta(hours=8))

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "query_trace.jsonl"

# 确保日志目录存在
os.makedirs(LOG_DIR, exist_ok=True)


# ============================================================
# Phase 常量（统一命名）
# ============================================================


class Phase:
    """节点埋点的 phase 字符串常量。

    所有 ``log_trace_event(phase=...)`` 调用必须引用此处常量，禁止裸字面量。
    新增 phase 时同步追加到 ``PHASES`` frozenset，便于 ``validate_phase()`` 校验。
    """

    # ── 路由 / 分类 ──
    ROUTING_INPUT = "routing_input"
    ROUTING_OUTPUT = "routing_output"
    CLASSIFIER_OUTPUT = "classifier_output"

    # ── Skill 执行 ──
    EXECUTION = "execution"
    EXECUTION_RESOLVE_NICKNAME = "execution_resolve_nickname"
    EXECUTION_CLARIFICATION_REQUESTED = "execution_clarification_requested"

    # ── 生成阶段 ──
    CONTEXT_BUILD = "context_build"
    GENERATION_INPUT = "generation_input"
    GENERATION_OUTPUT = "generation_output"
    AGENT_DETAIL = "agent_detail"

    # ── 澄清 / 多轮 ──
    CLARIFICATION_REQUESTED = "clarification_requested"
    MINOR_MERGE_SKIPPED = "minor_merge_skipped"
    MINOR_MERGE_FAILED = "minor_merge_failed"
    MINOR_MERGE_OUTPUT = "minor_merge_output"

    # ── 知识检索 ──
    ATLAS_SEARCH = "atlas_search"
    ATLAS_SEARCH_FALLBACK = "atlas_search_fallback"
    FACT_VERIFY = "fact_verify"
    GUIDE_SEARCH = "guide_search"

    # ── 会话持久化 ──
    SESSION_SAVE_TURN_FAILED = "session_save_turn_failed"
    PENDING_QUESTION_SAVED = "pending_question_saved"
    RESUME_LOADED = "resume_loaded"

    # ── 终态 ──
    RATING = "rating"
    FINAL = "final"

    # ── 前端事件 ──
    FRONTEND_FEEDBACK = "frontend_feedback"
    FRONTEND_VISIT = "frontend_visit"

    # ── 节点装饰器自动事件（with_trace 写入）──
    NODE_INPUT_SUFFIX = "_input"
    NODE_OUTPUT_SUFFIX = "_output"
    NODE_ERROR_SUFFIX = "_error"

    # ── 节点级 phase 前缀（仅供 @with_trace 装饰器使用，与业务 phase 命名隔离）──
    NODE_CLASSIFY = "node_classify"
    NODE_ROUTE = "node_route"
    NODE_EXECUTE = "node_execute"
    NODE_GENERATE = "node_generate"
    NODE_GUIDE = "node_guide"
    NODE_CLARIFY = "node_clarify"
    NODE_ATLAS = "node_atlas"
    NODE_FACT_VERIFY = "node_fact_verify"
    NODE_MERGE_FILTERS = "node_merge_filters"
    NODE_FALLBACK = "node_fallback"
    NODE_AGENT = "node_agent"


PHASES: frozenset[str] = frozenset(
    {
        Phase.ROUTING_INPUT,
        Phase.ROUTING_OUTPUT,
        Phase.CLASSIFIER_OUTPUT,
        Phase.EXECUTION,
        Phase.EXECUTION_RESOLVE_NICKNAME,
        Phase.EXECUTION_CLARIFICATION_REQUESTED,
        Phase.CONTEXT_BUILD,
        Phase.GENERATION_INPUT,
        Phase.GENERATION_OUTPUT,
        Phase.AGENT_DETAIL,
        Phase.CLARIFICATION_REQUESTED,
        Phase.MINOR_MERGE_SKIPPED,
        Phase.MINOR_MERGE_FAILED,
        Phase.MINOR_MERGE_OUTPUT,
        Phase.ATLAS_SEARCH,
        Phase.ATLAS_SEARCH_FALLBACK,
        Phase.FACT_VERIFY,
        Phase.GUIDE_SEARCH,
        Phase.SESSION_SAVE_TURN_FAILED,
        Phase.PENDING_QUESTION_SAVED,
        Phase.RESUME_LOADED,
        Phase.RATING,
        Phase.FINAL,
        Phase.FRONTEND_FEEDBACK,
        Phase.FRONTEND_VISIT,
        Phase.NODE_CLASSIFY,
        Phase.NODE_ROUTE,
        Phase.NODE_EXECUTE,
        Phase.NODE_GENERATE,
        Phase.NODE_GUIDE,
        Phase.NODE_CLARIFY,
        Phase.NODE_ATLAS,
        Phase.NODE_FACT_VERIFY,
        Phase.NODE_MERGE_FILTERS,
        Phase.NODE_FALLBACK,
        Phase.NODE_AGENT,
    }
)


def validate_phase(phase: str) -> bool:
    """校验 phase 是否在已声明集合中。

    装饰器自动产生的 ``{name}_input/_output/_error`` 事件视为合法。
    返回 False 时上层可选择记录 warning 但不阻塞主流程。
    """
    if phase in PHASES:
        return True
    for suffix in (Phase.NODE_INPUT_SUFFIX, Phase.NODE_OUTPUT_SUFFIX, Phase.NODE_ERROR_SUFFIX):
        if phase.endswith(suffix):
            return True
    return False


# ============================================================
# trace_id ContextVar（协程级自动传播）
# ============================================================


current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)


def bind_trace_id(trace_id: str) -> Token:
    """将 trace_id 绑定到当前 ContextVar，返回 reset Token。

    入口处（main.py / pipeline.py）必须显式调用，节点内通过 ``get_trace_id()``
    或 log_trace_event 缺省 trace_id 自动获取。
    """
    return current_trace_id.set(trace_id)


def reset_trace_id(token: Token) -> None:
    """通过 Token 还原 ContextVar（成对调用）。"""
    current_trace_id.reset(token)


def get_trace_id() -> str:
    """读取当前 ContextVar 中的 trace_id；未绑定时返回 ``"unknown"``。"""
    tid = current_trace_id.get()
    return tid if tid else "unknown"


# ============================================================
# 多阶段事件日志（新模式）
# ============================================================


def _build_trace_event(
    trace_id: str,
    phase: str,
    data: dict | None = None,
    error: str | None = None,
) -> dict:
    """构建单阶段事件数据。"""
    event = {
        "timestamp": datetime.now(_BEIJING_TZ).isoformat(),
        "traceId": trace_id,
        "phase": phase,
        "data": data or {},
    }
    if error:
        event["level"] = "ERROR"
        event["error"] = error
    return event


def _write_event_sync(event_data: dict):
    """同步写入单条事件到 JSONL 文件。"""
    line = json.dumps(event_data, ensure_ascii=False)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


async def log_trace_event(
    trace_id: str | None,
    phase: str,
    data: dict | None = None,
    error: str | None = None,
):
    """异步写入单阶段事件（通过线程池避免阻塞 Event Loop）。

    ``trace_id`` 传 None 时自动从 ContextVar ``current_trace_id`` 读取；如未
    绑定则回落为 ``"unknown"``。调用方通常应在请求入口先 ``bind_trace_id``，
    后续节点可显式传 None 让其自动传播。

    phase == "final" 时同步触发 BI 索引层 upsert（写 SQLite），失败仅记 ERROR
    日志，不影响 JSONL 落盘。
    """
    if trace_id is None:
        trace_id = get_trace_id()
    event = _build_trace_event(trace_id, phase, data, error)
    await asyncio.to_thread(_write_event_sync, event)
    if phase == Phase.FINAL and trace_id and trace_id != "unknown":
        # 延迟导入避免循环：bi_index 反向引用 logger.find_trace_events
        from server.bi_index import upsert_turn

        try:
            await asyncio.to_thread(upsert_turn, trace_id)
        except Exception:  # noqa: BLE001
            # upsert_turn 内部已 try/except，此处 belt-and-suspenders
            pass


def log_trace_event_sync(
    trace_id: str | None,
    phase: str,
    data: dict | None = None,
    error: str | None = None,
):
    """同步写入单阶段事件（供测试和非异步上下文使用）。"""
    if trace_id is None:
        trace_id = get_trace_id()
    event = _build_trace_event(trace_id, phase, data, error)
    _write_event_sync(event)


def find_trace_events(trace_id: str) -> list[dict]:
    """按 traceId 聚合查询所有阶段事件（按时间顺序）。"""
    if not LOG_FILE.exists():
        return []
    events = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("traceId") == trace_id:
                    events.append(entry)
            except json.JSONDecodeError:
                continue
    return events


# ============================================================
# 查询函数
# ============================================================


def read_traces(limit: int = 20) -> list[dict]:
    """读取最近 N 条 trace 日志（倒序，最新在前）。"""
    if not LOG_FILE.exists():
        return []
    traces = []
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    traces.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return traces[-limit:][::-1]


def find_trace(trace_id: str) -> dict | None:
    """按 traceId 查找 trace。

    优先聚合多阶段事件为完整视图；如无多阶段事件则返回旧模式单条 trace。
    """
    events = find_trace_events(trace_id)
    if not events:
        return None

    # 检查是否有 phase 字段（新模式）
    phased_events = [e for e in events if "phase" in e]
    if phased_events:
        # 聚合为完整视图
        result: dict = {"traceId": trace_id, "phases": phased_events}
        # 从 routing_input 提取 query
        for e in phased_events:
            if e.get("phase") == "routing_input":
                result["query"] = e.get("data", {}).get("query", "")
                break
        # 从 generation_output 提取 reply；agent_fallback 路径回退到 agent_detail
        for e in reversed(phased_events):
            if e.get("phase") == "generation_output":
                result["reply"] = e.get("data", {}).get("reply", e.get("data", {}).get("reply_preview", ""))
                break
        else:
            # agent_fallback 路径：从 agent_detail 提取 reply
            for e in reversed(phased_events):
                if e.get("phase") == "agent_detail":
                    agent_reply = e.get("data", {}).get("reply", "")
                    if agent_reply:
                        result["reply"] = agent_reply
                    break
        # 从 execution 提取 results_count
        for e in phased_events:
            if e.get("phase") == "execution":
                result["results_count"] = e.get("data", {}).get("total_found", 0)
                break
        # 从 routing_output 提取 intent
        for e in phased_events:
            if e.get("phase") == "routing_output":
                result["intent"] = {
                    "mode": "skill",
                    "skill_calls": e.get("data", {}).get("skill_calls", []),
                }
                break
        # 从 routing_input 提取 mode
        for e in phased_events:
            if e.get("phase") == "routing_input":
                result["mode"] = e.get("data", {}).get("mode", "")
                break
        # 从 clarification_requested / execution_clarification_requested 提取 clarification 信息
        for e in phased_events:
            if e.get("phase") in ("clarification_requested", "execution_clarification_requested"):
                result["clarification"] = {
                    "question": e.get("data", {}).get("question", ""),
                    "options": e.get("data", {}).get("options", []),
                    "ambiguous_field": e.get("data", {}).get("ambiguous_field", ""),
                    "source": "execution" if e.get("phase") == "execution_clarification_requested" else "routing",
                }
                # clarification 模式下 reply 是问题本身
                if "reply" not in result:
                    result["reply"] = e.get("data", {}).get("question", "")
                break
        # 从 routing_input 提取 is_confirmation 标记
        for e in phased_events:
            if e.get("phase") == "routing_input":
                if e.get("data", {}).get("is_confirmation"):
                    result["is_confirmation"] = True
                    result["confirmation_context"] = e.get("data", {}).get("confirmation_context", "")
                break
        # 从 final 提取 mode（优先）和 total_tokens
        for e in phased_events:
            if e.get("phase") == "final":
                final_data = e.get("data", {})
                if final_data.get("mode"):
                    result["mode"] = final_data["mode"]
                result["total_tokens"] = final_data.get("total_tokens")
                break
        return result

    # 旧模式：返回最后一条匹配的 entry
    return events[-1]


def read_trace_summaries(
    limit: int = 50,
    offset: int = 0,
    keyword: str | None = None,
    rating: str | None = None,
) -> dict:
    """按 traceId 聚合日志，返回摘要列表（分页 + 可选关键词/评分过滤）。

    Args:
        rating: 可选评分筛选，值为 "good" / "ok" / "bad"

    Returns:
        {"total": int, "items": [{"traceId", "timestamp", "query", "status", "duration_ms"}, ...]}
    """
    if not LOG_FILE.exists():
        return {"total": 0, "items": []}

    # 1. 读取所有行并按 traceId 分组
    from collections import OrderedDict

    groups: OrderedDict[str, list[dict]] = OrderedDict()
    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = entry.get("traceId")
            if not tid:
                continue
            groups.setdefault(tid, []).append(entry)

    # 2. 提取每个 traceId 的摘要
    summaries: list[dict] = []
    for tid, events in groups.items():
        query = ""
        status = "unknown"
        duration_ms = None
        timestamp = events[0].get("timestamp", "")
        error_msg = None
        mode = None
        total_tokens = None
        trace_rating = None
        is_confirmation = False
        clarification_question = ""

        for e in events:
            phase = e.get("phase", "")
            data = e.get("data", {})
            if phase == "routing_input":
                query = data.get("query", "")
                timestamp = e.get("timestamp", timestamp)
                if data.get("is_confirmation"):
                    is_confirmation = True
            elif phase == "rating":
                trace_rating = data.get("rating")
            elif phase in ("clarification_requested", "execution_clarification_requested"):
                clarification_question = data.get("question", "")
            elif phase == "final":
                status = data.get("result", "unknown")
                duration_ms = data.get("total_time_ms")
                mode = data.get("mode")
                total_tokens = data.get("total_tokens")
                if e.get("error"):
                    error_msg = e["error"][:200]
            # 旧模式兼容
            if not query and "query" in e:
                query = e["query"]
            if e.get("level") == "ERROR" and status == "unknown":
                status = "error"
                error_msg = e.get("error", "")[:200]

        if status == "unknown" and not error_msg:
            status = "success"

        summary: dict = {
            "traceId": tid,
            "timestamp": timestamp,
            "query": query,
            "status": status,
            "duration_ms": round(duration_ms, 1) if duration_ms else None,
            "error": error_msg,
            "mode": mode,
            "total_tokens": total_tokens,
            "rating": trace_rating,
        }
        if is_confirmation:
            summary["is_confirmation"] = True
        if clarification_question:
            summary["clarification_question"] = clarification_question
        summaries.append(summary)

    # 3. 按时间倒序（最新在前）
    summaries.reverse()

    # 4. 关键词过滤
    if keyword:
        kw = keyword.lower()
        summaries = [
            s
            for s in summaries
            if kw in s.get("query", "").lower()
            or kw in s.get("traceId", "").lower()
            or kw in (s.get("error") or "").lower()
        ]

    # 5. 评分过滤
    if rating:
        summaries = [s for s in summaries if s.get("rating") == rating]

    total = len(summaries)
    items = summaries[offset : offset + limit]

    return {"total": total, "items": items}


# ============================================================
# 统计汇总（BI 面板用）
# ============================================================


def compute_log_stats(days: int = 7) -> dict:
    """计算最近 N 天的日志统计数据。

    v0.5.1 起改走 SQLite 索引层（``server.bi_index.query_stats``），输出 schema
    与历史完全一致以兼容 admin 后台。SQLite 缺失或聚合异常时回退到 JSONL 全表
    扫描（_legacy 实现）。
    """
    try:
        from server.bi_index import DB_PATH, query_stats

        if DB_PATH.exists():
            return query_stats(days=days)
    except Exception:  # noqa: BLE001
        # bi_index 异常时不阻塞 admin，回退到 legacy 路径
        pass
    return _compute_log_stats_legacy(days=days)


def _compute_log_stats_legacy(days: int = 7) -> dict:
    """旧 JSONL 全表扫描实现（fallback / 单测对照用）。"""
    from collections import defaultdict

    if not LOG_FILE.exists():
        return {
            "pv": 0,
            "uv": 0,
            "paths": [],
            "daily": [],
            "ratings": {"bad": 0, "ok": 0, "good": 0},
            "modes": [],
        }

    cutoff = datetime.now(_BEIJING_TZ) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    # 按 traceId 分组
    trace_data: dict[str, dict] = {}  # traceId -> aggregated info
    ratings = {"bad": 0, "ok": 0, "good": 0}

    with open(LOG_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            timestamp = entry.get("timestamp", "")
            if timestamp < cutoff_str:
                continue

            tid = entry.get("traceId")
            if not tid:
                continue

            phase = entry.get("phase", "")

            # 评分事件单独计数
            if phase == "rating":
                rating_value = entry.get("data", {}).get("rating", "")
                if rating_value in ratings:
                    ratings[rating_value] += 1
                continue

            # 首次出现的 trace 初始化
            if tid not in trace_data:
                trace_data[tid] = {
                    "date": timestamp[:10],  # YYYY-MM-DD
                    "ip": "",
                    "query": "",
                    "mode": "",
                }

            data = entry.get("data", {})
            if phase == "routing_input":
                trace_data[tid]["query"] = data.get("query", "")
                trace_data[tid]["ip"] = data.get("client_ip", "")
            elif phase == "final":
                trace_data[tid]["mode"] = data.get("mode", "")

            # 旧模式兼容
            if not trace_data[tid]["query"] and "query" in entry:
                trace_data[tid]["query"] = entry["query"]

    # 统计汇总
    daily_pv: dict[str, int] = defaultdict(int)
    daily_ips: dict[str, set] = defaultdict(set)
    mode_counts: dict[str, int] = defaultdict(int)
    path_counts: dict[str, int] = defaultdict(int)
    all_ips: set = set()

    for tid, info in trace_data.items():
        date = info["date"]
        ip = info["ip"] or "unknown"  # 无 IP 时统一归为同一未知用户
        query = info["query"]
        mode = info["mode"] or "unknown"

        daily_pv[date] += 1
        daily_ips[date].add(ip)
        all_ips.add(ip)
        mode_counts[mode] += 1

        # 路径统计：使用 query 的前 20 字符作为"路径"
        path_key = query[:20] if query else "(empty)"
        path_counts[path_key] += 1

    # 组装结果
    pv = len(trace_data)
    uv = len(all_ips)

    # 日期趋势（按日期排序）
    daily = sorted(
        [{"date": d, "pv": daily_pv[d], "uv": len(daily_ips[d])} for d in daily_pv],
        key=lambda x: x["date"],
    )

    # 路径分布 Top 10
    paths = sorted(
        [{"path": p, "count": c} for p, c in path_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    # 模式分布
    modes = sorted(
        [{"mode": m, "count": c} for m, c in mode_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )

    return {
        "pv": pv,
        "uv": uv,
        "paths": paths,
        "daily": daily,
        "ratings": ratings,
        "modes": modes,
    }
