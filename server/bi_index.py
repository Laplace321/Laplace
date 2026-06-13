"""
Laplace — BI 索引层（SQLite）

JSONL 仍为事实源（``server/logs/query_trace.jsonl``），SQLite 仅作为
索引/聚合查询层。schema 设计要点：

- 一行 = 一次完整请求（trace_id 维度），由 ``final`` 事件触发写入
- 字段通过聚合该 traceId 下的所有 phased events 抽取（routing_input、
  classifier_output、execution、final、rating 等）
- 任意写入失败不阻塞主流程；JSONL 始终保留可重建数据

API：
- ``upsert_turn(trace_id)``           ── final 事件触发，单 trace 增量更新
- ``reindex_from_jsonl(...)``         ── 全量重建（清表后扫 JSONL）
- ``query_stats(...)``                ── compute_log_stats 兼容输出
- CLI: ``python -m server.bi_index reindex``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.logger import _BEIJING_TZ, LOG_DIR, LOG_FILE, find_trace_events

logger = logging.getLogger(__name__)

DB_PATH = LOG_DIR / "bi_index.sqlite"


# ============================================================
# Schema
# ============================================================


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS turn_summary (
    trace_id           TEXT PRIMARY KEY,
    ts                 TEXT NOT NULL,           -- ISO8601（routing_input 的 timestamp 优先，缺省回退 final）
    session_id         TEXT,
    turn_type          TEXT,                    -- MAJOR / MINOR / RESUME / unknown
    pipeline           TEXT,                    -- A / B / C / agent / preset / confirmation / fallback / direct / unknown
    skill_names        TEXT,                    -- 逗号分隔有序字符串
    clarification_type TEXT,                    -- routing / execution / null
    error_reason       TEXT,                    -- 异常路径 reason；null 表示成功
    latency_ms         REAL,
    total_tokens       INTEGER,
    rating             TEXT,                    -- bad / ok / good / null
    model              TEXT,
    query_hash         TEXT,                    -- sha256(query) 前 16 位
    query_preview      TEXT,                    -- query 前 60 字符
    client_ip          TEXT,
    mode               TEXT,                    -- final.mode 字段
    is_confirmation    INTEGER DEFAULT 0,
    status             TEXT                     -- success / routing_error / stream_error / ...
);

CREATE INDEX IF NOT EXISTS idx_turn_ts                 ON turn_summary(ts);
CREATE INDEX IF NOT EXISTS idx_turn_pipeline_type      ON turn_summary(pipeline, turn_type);
CREATE INDEX IF NOT EXISTS idx_turn_error_reason       ON turn_summary(error_reason);
CREATE INDEX IF NOT EXISTS idx_turn_session_ts         ON turn_summary(session_id, ts);
"""


def _connect() -> sqlite3.Connection:
    """打开/创建 SQLite 连接（关闭由调用方负责）。

    每次 upsert/query 走一个新连接，避免线程安全问题；SQLite 内部 page cache
    足够支撑当前量级的写入频率。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    conn.commit()


# ============================================================
# 事件聚合 → 行字段
# ============================================================


def _hash_query(query: str) -> str:
    """query → 16 位 sha256 前缀，便于去重/聚合而不暴露明文。"""
    if not query:
        return ""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def _aggregate_events_to_row(trace_id: str, events: list[dict]) -> dict[str, Any] | None:
    """把同一 trace_id 下的所有 phased events 聚合为 turn_summary 单行。

    返回 None 表示当前 events 还没有 final，跳过 upsert（避免半成品入索引）。
    """
    if not events:
        return None

    row: dict[str, Any] = {
        "trace_id": trace_id,
        "ts": events[0].get("timestamp", ""),
        "session_id": None,
        "turn_type": None,
        "pipeline": None,
        "skill_names": None,
        "clarification_type": None,
        "error_reason": None,
        "latency_ms": None,
        "total_tokens": None,
        "rating": None,
        "model": None,
        "query_hash": None,
        "query_preview": None,
        "client_ip": None,
        "mode": None,
        "is_confirmation": 0,
        "status": None,
    }

    has_final = False

    for e in events:
        phase = e.get("phase", "")
        data = e.get("data", {}) or {}

        if phase == "routing_input":
            query = data.get("query", "") or ""
            row["query_hash"] = _hash_query(query)
            row["query_preview"] = query[:60]
            row["client_ip"] = data.get("client_ip")
            row["ts"] = e.get("timestamp", row["ts"])
            if data.get("is_confirmation"):
                row["is_confirmation"] = 1
        elif phase == "classifier_output":
            # turn_type / pipeline 主要写在 classifier 后（兜底用 final.metric_labels）
            row["turn_type"] = data.get("turn_type") or row["turn_type"]
            row["pipeline"] = data.get("pipeline") or row["pipeline"]
        elif phase == "rating":
            row["rating"] = data.get("rating")
        elif phase == "final":
            has_final = True
            row["latency_ms"] = data.get("total_time_ms") or row["latency_ms"]
            row["total_tokens"] = data.get("total_tokens") or row["total_tokens"]
            row["status"] = data.get("result") or row["status"]
            row["mode"] = data.get("mode") or row["mode"]
            labels = data.get("metric_labels") or {}
            if isinstance(labels, dict):
                row["pipeline"] = labels.get("pipeline") or row["pipeline"]
                row["turn_type"] = labels.get("turn_type") or row["turn_type"]
                row["skill_names"] = labels.get("skill_names") or row["skill_names"]
                row["clarification_type"] = labels.get("clarification_type") or row["clarification_type"]
                row["error_reason"] = labels.get("error_reason") or row["error_reason"]
                row["model"] = labels.get("model") or row["model"]
                # total_tokens label 类型可能是 int/str，与 final.total_tokens 二选一即可
                if row["total_tokens"] is None:
                    tk = labels.get("total_tokens")
                    if isinstance(tk, (int, float)):
                        row["total_tokens"] = int(tk)

    if not has_final:
        return None

    # session_id 通常不在 phased events 里，从 events[0] 顶层字段尝试取（旧模式兼容）
    for e in events:
        if e.get("session_id"):
            row["session_id"] = e["session_id"]
            break
        sid = (e.get("data") or {}).get("session_id")
        if sid:
            row["session_id"] = sid
            break

    return row


# ============================================================
# Upsert
# ============================================================


_UPSERT_SQL = """
INSERT INTO turn_summary (
    trace_id, ts, session_id, turn_type, pipeline, skill_names, clarification_type,
    error_reason, latency_ms, total_tokens, rating, model, query_hash, query_preview,
    client_ip, mode, is_confirmation, status
) VALUES (
    :trace_id, :ts, :session_id, :turn_type, :pipeline, :skill_names, :clarification_type,
    :error_reason, :latency_ms, :total_tokens, :rating, :model, :query_hash, :query_preview,
    :client_ip, :mode, :is_confirmation, :status
)
ON CONFLICT(trace_id) DO UPDATE SET
    ts                 = excluded.ts,
    session_id         = COALESCE(excluded.session_id, turn_summary.session_id),
    turn_type          = COALESCE(excluded.turn_type, turn_summary.turn_type),
    pipeline           = COALESCE(excluded.pipeline, turn_summary.pipeline),
    skill_names        = COALESCE(excluded.skill_names, turn_summary.skill_names),
    clarification_type = COALESCE(excluded.clarification_type, turn_summary.clarification_type),
    error_reason       = COALESCE(excluded.error_reason, turn_summary.error_reason),
    latency_ms         = COALESCE(excluded.latency_ms, turn_summary.latency_ms),
    total_tokens       = COALESCE(excluded.total_tokens, turn_summary.total_tokens),
    rating             = COALESCE(excluded.rating, turn_summary.rating),
    model              = COALESCE(excluded.model, turn_summary.model),
    query_hash         = COALESCE(excluded.query_hash, turn_summary.query_hash),
    query_preview      = COALESCE(excluded.query_preview, turn_summary.query_preview),
    client_ip          = COALESCE(excluded.client_ip, turn_summary.client_ip),
    mode               = COALESCE(excluded.mode, turn_summary.mode),
    is_confirmation    = MAX(excluded.is_confirmation, turn_summary.is_confirmation),
    status             = COALESCE(excluded.status, turn_summary.status)
"""


def upsert_turn(trace_id: str) -> bool:
    """聚合该 trace_id 下的所有 events → upsert 到 turn_summary。

    - final 事件未到时直接返回 False（不写半成品）
    - 任何异常被捕获并 logger.error，不向上抛
    - 返回 True 表示成功写入或更新
    """
    if not trace_id:
        return False
    try:
        events = find_trace_events(trace_id)
        row = _aggregate_events_to_row(trace_id, events)
        if row is None:
            return False
        with _connect() as conn:
            _ensure_schema(conn)
            conn.execute(_UPSERT_SQL, row)
            conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("bi_index.upsert_turn failed: trace_id=%s err=%s", trace_id, exc)
        return False


# ============================================================
# Reindex（全量重建）
# ============================================================


def reindex_from_jsonl(jsonl_path: Path | None = None, *, drop_first: bool = True) -> dict[str, int]:
    """从 JSONL 重建整个 turn_summary 索引。

    Args:
        jsonl_path: 默认读 ``server/logs/query_trace.jsonl``
        drop_first: True 表示先 ``DELETE FROM`` 再批量 upsert（推荐）

    Returns:
        { "scanned_lines": int, "indexed_traces": int, "skipped_no_final": int }
    """
    src = jsonl_path or LOG_FILE
    stats = {"scanned_lines": 0, "indexed_traces": 0, "skipped_no_final": 0}
    if not src.exists():
        return stats

    groups: OrderedDict[str, list[dict]] = OrderedDict()
    with open(src, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            stats["scanned_lines"] += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = entry.get("traceId")
            if not tid:
                continue
            groups.setdefault(tid, []).append(entry)

    with _connect() as conn:
        _ensure_schema(conn)
        if drop_first:
            conn.execute("DELETE FROM turn_summary")
        rows = []
        for tid, events in groups.items():
            row = _aggregate_events_to_row(tid, events)
            if row is None:
                stats["skipped_no_final"] += 1
                continue
            rows.append(row)
        if rows:
            conn.executemany(_UPSERT_SQL, rows)
            stats["indexed_traces"] = len(rows)
        conn.commit()

    return stats


# ============================================================
# 查询接口（compute_log_stats 兼容）
# ============================================================


def query_stats(days: int = 7) -> dict:
    """从 SQLite 聚合最近 N 天的统计，输出 schema 与
    ``logger.compute_log_stats`` 完全兼容（admin 后台不感知切换）。
    """
    cutoff = datetime.now(_BEIJING_TZ) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    empty = {
        "pv": 0,
        "uv": 0,
        "paths": [],
        "daily": [],
        "ratings": {"bad": 0, "ok": 0, "good": 0},
        "modes": [],
    }

    if not DB_PATH.exists():
        return empty

    try:
        with _connect() as conn:
            _ensure_schema(conn)

            cur = conn.cursor()

            # PV / UV
            cur.execute(
                "SELECT COUNT(*) AS pv, COUNT(DISTINCT COALESCE(client_ip, 'unknown')) AS uv "
                "FROM turn_summary WHERE ts >= ?",
                (cutoff_str,),
            )
            r = cur.fetchone()
            pv = r["pv"] if r else 0
            uv = r["uv"] if r else 0
            if pv == 0:
                return empty

            # 每日趋势
            cur.execute(
                "SELECT substr(ts, 1, 10) AS d, "
                "       COUNT(*) AS pv, "
                "       COUNT(DISTINCT COALESCE(client_ip, 'unknown')) AS uv "
                "FROM turn_summary WHERE ts >= ? GROUP BY d ORDER BY d",
                (cutoff_str,),
            )
            daily = [{"date": row["d"], "pv": row["pv"], "uv": row["uv"]} for row in cur.fetchall()]

            # 模式分布
            cur.execute(
                "SELECT COALESCE(mode, 'unknown') AS m, COUNT(*) AS c "
                "FROM turn_summary WHERE ts >= ? GROUP BY m ORDER BY c DESC",
                (cutoff_str,),
            )
            modes = [{"mode": row["m"], "count": row["c"]} for row in cur.fetchall()]

            # 路径分布（query_preview 前 20 字符 — 与旧 compute_log_stats 一致）
            cur.execute(
                "SELECT substr(COALESCE(query_preview, ''), 1, 20) AS p, COUNT(*) AS c "
                "FROM turn_summary WHERE ts >= ? GROUP BY p ORDER BY c DESC LIMIT 10",
                (cutoff_str,),
            )
            paths = [{"path": row["p"] or "(empty)", "count": row["c"]} for row in cur.fetchall()]

            # 评分分布
            ratings: dict[str, int] = {"bad": 0, "ok": 0, "good": 0}
            cur.execute(
                "SELECT rating, COUNT(*) AS c FROM turn_summary WHERE ts >= ? AND rating IS NOT NULL GROUP BY rating",
                (cutoff_str,),
            )
            for row in cur.fetchall():
                rk = row["rating"]
                if rk in ratings:
                    ratings[rk] = row["c"]

        return {
            "pv": pv,
            "uv": uv,
            "paths": paths,
            "daily": daily,
            "ratings": ratings,
            "modes": modes,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("bi_index.query_stats failed: %s", exc)
        return empty


def query_dimension_stats(days: int = 7) -> dict:
    """新维度聚合：按 pipeline / turn_type / skill_name / error_reason 切分。

    供 admin 后台或排障使用。返回：
        {
            "by_pipeline": [{"pipeline", "count", "error_count", "avg_latency_ms"}, ...],
            "by_turn_type": [{"turn_type", "count"}, ...],
            "by_skill": [{"skill_name", "count"}, ...],   # skill_names 字段拆分
            "by_error_reason": [{"error_reason", "count"}, ...],
        }
    """
    cutoff = datetime.now(_BEIJING_TZ) - timedelta(days=days)
    cutoff_str = cutoff.isoformat()

    empty = {"by_pipeline": [], "by_turn_type": [], "by_skill": [], "by_error_reason": []}
    if not DB_PATH.exists():
        return empty

    try:
        with _connect() as conn:
            _ensure_schema(conn)
            cur = conn.cursor()

            cur.execute(
                "SELECT COALESCE(pipeline, 'unknown') AS p, COUNT(*) AS c, "
                "       SUM(CASE WHEN error_reason IS NOT NULL THEN 1 ELSE 0 END) AS ec, "
                "       AVG(latency_ms) AS al "
                "FROM turn_summary WHERE ts >= ? GROUP BY p ORDER BY c DESC",
                (cutoff_str,),
            )
            by_pipeline = [
                {
                    "pipeline": row["p"],
                    "count": row["c"],
                    "error_count": row["ec"] or 0,
                    "avg_latency_ms": round(row["al"], 1) if row["al"] is not None else None,
                }
                for row in cur.fetchall()
            ]

            cur.execute(
                "SELECT COALESCE(turn_type, 'unknown') AS t, COUNT(*) AS c "
                "FROM turn_summary WHERE ts >= ? GROUP BY t ORDER BY c DESC",
                (cutoff_str,),
            )
            by_turn_type = [{"turn_type": row["t"], "count": row["c"]} for row in cur.fetchall()]

            cur.execute(
                "SELECT skill_names FROM turn_summary WHERE ts >= ? AND skill_names IS NOT NULL",
                (cutoff_str,),
            )
            skill_counter: dict[str, int] = defaultdict(int)
            for row in cur.fetchall():
                names = (row["skill_names"] or "").split(",")
                for n in names:
                    n = n.strip()
                    if n:
                        skill_counter[n] += 1
            by_skill = sorted(
                [{"skill_name": k, "count": v} for k, v in skill_counter.items()],
                key=lambda x: x["count"],
                reverse=True,
            )

            cur.execute(
                "SELECT error_reason AS er, COUNT(*) AS c FROM turn_summary "
                "WHERE ts >= ? AND error_reason IS NOT NULL GROUP BY er ORDER BY c DESC",
                (cutoff_str,),
            )
            by_error_reason = [{"error_reason": row["er"], "count": row["c"]} for row in cur.fetchall()]

        return {
            "by_pipeline": by_pipeline,
            "by_turn_type": by_turn_type,
            "by_skill": by_skill,
            "by_error_reason": by_error_reason,
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("bi_index.query_dimension_stats failed: %s", exc)
        return empty


# ============================================================
# CLI
# ============================================================


def _main() -> int:
    parser = argparse.ArgumentParser(prog="python -m server.bi_index")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_reindex = sub.add_parser("reindex", help="从 JSONL 全量重建 SQLite 索引")
    p_reindex.add_argument("--no-drop", action="store_true", help="保留现有数据，仅做 upsert")
    sub.add_parser("stats", help="打印最近 7 天 PV/UV 摘要")

    args = parser.parse_args()
    if args.cmd == "reindex":
        result = reindex_from_jsonl(drop_first=not args.no_drop)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        print(json.dumps(query_stats(days=7), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
