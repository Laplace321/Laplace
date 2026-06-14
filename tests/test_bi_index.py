"""BI 索引层（SQLite）测试。

覆盖：
- ``upsert_turn`` 幂等性（同 trace_id 多次调用结果一致）
- ``upsert_turn`` 在缺 final 事件时跳过（不写半成品）
- ``upsert_turn`` 异常路径不抛（容错）
- ``reindex_from_jsonl`` 全量重建一致性
- ``query_stats`` schema 与 ``logger.compute_log_stats`` 兼容
- ``query_dimension_stats`` 维度切分（pipeline / turn_type / skill_name / error_reason）
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

# ============================================================
# Fixture：临时 LOG_DIR + DB_PATH
# ============================================================


@pytest.fixture
def tmp_log_dir(tmp_path: Path):
    """提供独立的 LOG_DIR + bi_index DB_PATH，避免污染真实日志/索引。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    db_path = log_dir / "bi_index.sqlite"

    with (
        patch("server.logger.LOG_DIR", log_dir),
        patch("server.bi_index.LOG_DIR", log_dir),
        patch("server.bi_index.DB_PATH", db_path),
    ):
        yield log_dir, db_path


def _write_jsonl(log_dir: Path, filename: str, events: list[dict]) -> Path:
    """把 events 序列写入指定 JSONL 文件，每行一个事件。"""
    path = log_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")
    return path


def _make_events(
    trace_id: str,
    *,
    pipeline: str = "A",
    turn_type: str = "MAJOR",
    skill_names: list[str] | None = None,
    latency_ms: float = 123.4,
    total_tokens: int = 256,
    rating: str | None = None,
    error_reason: str | None = None,
    query: str = "充能技能",
    timestamp: str = "2026-06-12T10:00:00+08:00",
    session_id: str = "sess-test",
    model: str = "gpt-4o-mini",
) -> list[dict]:
    """构造一组完整的 trace events（routing_input + execution + final + 可选 rating）。"""
    skill_names = skill_names or ["search_by_effect"]
    events: list[dict] = [
        {
            "traceId": trace_id,
            "phase": "routing_input",
            "timestamp": timestamp,
            "data": {
                "query": query,
                "session_id": session_id,
                "client_ip": "127.0.0.1",
            },
        },
        {
            "traceId": trace_id,
            "phase": "classifier_output",
            "timestamp": timestamp,
            "data": {"turn_type": turn_type, "pipeline": pipeline},
        },
        {
            "traceId": trace_id,
            "phase": "execution",
            "timestamp": timestamp,
            "data": {"skill_names": skill_names},
        },
    ]
    final_data = {
        "metric_labels": {
            "pipeline": pipeline,
            "turn_type": turn_type,
            "skill_names": ",".join(skill_names),
            "model": model,
            "total_tokens": total_tokens,
        },
        "total_time_ms": latency_ms,
        "total_tokens": total_tokens,
        "mode": "json",
    }
    if error_reason:
        final_data["metric_labels"]["error_reason"] = error_reason
    events.append(
        {
            "traceId": trace_id,
            "phase": "final",
            "timestamp": timestamp,
            "data": final_data,
        }
    )
    if rating:
        events.append(
            {
                "traceId": trace_id,
                "phase": "rating",
                "timestamp": timestamp,
                "data": {"rating": rating},
            }
        )
    return events


# ============================================================
# upsert_turn
# ============================================================


class TestUpsertTurn:
    def test_upsert_creates_row(self, tmp_log_dir):
        log_dir, db_path = tmp_log_dir
        events = _make_events("trace-001")
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", events)

        from server import bi_index

        ok = bi_index.upsert_turn("trace-001")
        assert ok is True
        assert db_path.exists()

        # 验证行字段
        with bi_index._connect() as conn:
            row = conn.execute("SELECT * FROM turn_summary WHERE trace_id=?", ("trace-001",)).fetchone()
        assert row is not None
        assert row["trace_id"] == "trace-001"
        assert row["pipeline"] == "A"
        assert row["turn_type"] == "MAJOR"
        assert row["skill_names"] == "search_by_effect"
        assert row["latency_ms"] == pytest.approx(123.4)
        assert row["total_tokens"] == 256

    def test_upsert_idempotent(self, tmp_log_dir):
        """同 trace_id 调用两次，行数仍为 1，字段一致。"""
        log_dir, _ = tmp_log_dir
        events = _make_events("trace-002")
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", events)

        from server import bi_index

        assert bi_index.upsert_turn("trace-002") is True
        assert bi_index.upsert_turn("trace-002") is True

        with bi_index._connect() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM turn_summary").fetchone()["c"]
        assert count == 1

    def test_upsert_skips_when_no_final(self, tmp_log_dir):
        """final 事件未到时返回 False，不写入半成品。"""
        log_dir, db_path = tmp_log_dir
        # 只有 routing_input，没 final
        events = _make_events("trace-003")[:1]
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", events)

        from server import bi_index

        ok = bi_index.upsert_turn("trace-003")
        assert ok is False

        # 表可能存在（schema 已建），但应无对应行
        if db_path.exists():
            with bi_index._connect() as conn:
                row = conn.execute("SELECT * FROM turn_summary WHERE trace_id=?", ("trace-003",)).fetchone()
            assert row is None

    def test_upsert_empty_trace_id_returns_false(self, tmp_log_dir):
        from server import bi_index

        assert bi_index.upsert_turn("") is False

    def test_upsert_swallows_exception(self, tmp_log_dir):
        """find_trace_events 抛异常时 upsert 返回 False，不向上抛。"""
        from server import bi_index

        with patch("server.bi_index.find_trace_events", side_effect=RuntimeError("boom")):
            assert bi_index.upsert_turn("trace-x") is False


# ============================================================
# reindex_from_jsonl
# ============================================================


class TestReindexFromJsonl:
    def test_rebuild_from_multi_files(self, tmp_log_dir):
        """多天 JSONL → 全量重建。"""
        log_dir, _ = tmp_log_dir
        e1 = _make_events("t-day1", timestamp="2026-06-10T10:00:00+08:00")
        e2 = _make_events(
            "t-day2",
            pipeline="B",
            turn_type="MINOR",
            skill_names=["search_by_class"],
            timestamp="2026-06-11T10:00:00+08:00",
        )
        _write_jsonl(log_dir, "query_trace.2026-06-10.jsonl", e1)
        _write_jsonl(log_dir, "query_trace.2026-06-11.jsonl", e2)

        from server import bi_index

        stats = bi_index.reindex_from_jsonl()
        assert stats["scanned_lines"] >= 8  # 2 trace × 4 events
        assert stats["indexed_traces"] == 2

        with bi_index._connect() as conn:
            rows = conn.execute("SELECT trace_id, pipeline, turn_type FROM turn_summary ORDER BY trace_id").fetchall()
        assert [r["trace_id"] for r in rows] == ["t-day1", "t-day2"]
        assert rows[0]["pipeline"] == "A"
        assert rows[1]["pipeline"] == "B"
        assert rows[1]["turn_type"] == "MINOR"

    def test_rebuild_drops_existing(self, tmp_log_dir):
        """drop_first=True 时清空旧数据。"""
        log_dir, _ = tmp_log_dir
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", _make_events("t-old"))

        from server import bi_index

        assert bi_index.upsert_turn("t-old") is True

        # 现在 JSONL 改为只剩 t-new
        log_dir.joinpath("query_trace.2026-06-12.jsonl").unlink()
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", _make_events("t-new"))

        stats = bi_index.reindex_from_jsonl(drop_first=True)
        assert stats["indexed_traces"] == 1

        with bi_index._connect() as conn:
            rows = conn.execute("SELECT trace_id FROM turn_summary").fetchall()
        assert [r["trace_id"] for r in rows] == ["t-new"]

    def test_rebuild_skips_no_final(self, tmp_log_dir):
        """没 final 事件的 trace 计入 skipped_no_final。"""
        log_dir, _ = tmp_log_dir
        complete = _make_events("t-complete")
        partial = _make_events("t-partial")[:2]  # 缺 final
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", complete + partial)

        from server import bi_index

        stats = bi_index.reindex_from_jsonl()
        assert stats["indexed_traces"] == 1
        assert stats["skipped_no_final"] == 1

    def test_rebuild_handles_corrupt_jsonl(self, tmp_log_dir):
        """JSONL 中夹杂坏行不影响其他 trace 入库。"""
        log_dir, _ = tmp_log_dir
        path = log_dir / "query_trace.2026-06-12.jsonl"
        good = _make_events("t-good")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-a-json-line\n")
            for evt in good:
                f.write(json.dumps(evt) + "\n")
            f.write("{broken json\n")

        from server import bi_index

        stats = bi_index.reindex_from_jsonl()
        assert stats["indexed_traces"] == 1


# ============================================================
# query_stats（compute_log_stats 兼容）
# ============================================================


class TestQueryStats:
    def test_empty_when_no_data(self, tmp_log_dir):
        from server import bi_index

        result = bi_index.query_stats(days=7)
        assert result == {
            "pv": 0,
            "uv": 0,
            "paths": [],
            "daily": [],
            "ratings": {"bad": 0, "ok": 0, "good": 0},
            "modes": [],
        }

    def test_aggregates_pv_uv_modes_ratings(self, tmp_log_dir):
        from datetime import datetime

        log_dir, _ = tmp_log_dir
        # 2 trace 同一天，1 trace 有 rating=good
        ts = datetime.now().astimezone().isoformat()
        e1 = _make_events("t-a", timestamp=ts, rating="good")
        e2 = _make_events("t-b", timestamp=ts, query="爆发技能")
        _write_jsonl(log_dir, "query_trace.today.jsonl", e1 + e2)

        from server import bi_index

        bi_index.reindex_from_jsonl()
        result = bi_index.query_stats(days=30)

        assert result["pv"] == 2
        assert result["uv"] >= 1
        # schema 兼容
        assert set(result.keys()) == {"pv", "uv", "paths", "daily", "ratings", "modes"}
        assert result["ratings"]["good"] == 1
        assert result["ratings"]["bad"] == 0
        # 模式分布有数据
        assert any(m["mode"] == "json" for m in result["modes"])
        # 每日趋势有数据
        assert len(result["daily"]) >= 1


# ============================================================
# query_dimension_stats（新维度）
# ============================================================


class TestQueryDimensionStats:
    def test_empty_when_no_data(self, tmp_log_dir):
        from server import bi_index

        result = bi_index.query_dimension_stats(days=7)
        assert result == {
            "by_pipeline": [],
            "by_turn_type": [],
            "by_skill": [],
            "by_error_reason": [],
        }

    def test_dimension_aggregation(self, tmp_log_dir):
        from datetime import datetime

        log_dir, _ = tmp_log_dir
        ts = datetime.now().astimezone().isoformat()
        # 3 trace：A/MAJOR/skill1+skill2、B/MINOR/skill1、A/MAJOR/skill1（含错误）
        events = []
        events += _make_events("t1", pipeline="A", turn_type="MAJOR", skill_names=["s1", "s2"], timestamp=ts)
        events += _make_events("t2", pipeline="B", turn_type="MINOR", skill_names=["s1"], timestamp=ts)
        events += _make_events(
            "t3",
            pipeline="A",
            turn_type="MAJOR",
            skill_names=["s1"],
            timestamp=ts,
            error_reason="stream_error",
        )
        _write_jsonl(log_dir, "query_trace.today.jsonl", events)

        from server import bi_index

        bi_index.reindex_from_jsonl()
        result = bi_index.query_dimension_stats(days=30)

        # by_pipeline
        pipe_map = {row["pipeline"]: row for row in result["by_pipeline"]}
        assert pipe_map["A"]["count"] == 2
        assert pipe_map["A"]["error_count"] == 1
        assert pipe_map["B"]["count"] == 1

        # by_turn_type
        type_map = {row["turn_type"]: row["count"] for row in result["by_turn_type"]}
        assert type_map["MAJOR"] == 2
        assert type_map["MINOR"] == 1

        # by_skill：s1 出现 3 次，s2 出现 1 次
        skill_map = {row["skill_name"]: row["count"] for row in result["by_skill"]}
        assert skill_map["s1"] == 3
        assert skill_map["s2"] == 1

        # by_error_reason
        err_map = {row["error_reason"]: row["count"] for row in result["by_error_reason"]}
        assert err_map.get("stream_error") == 1


# ============================================================
# 端到端：JSONL 仍是事实源
# ============================================================


class TestSourceOfTruth:
    def test_jsonl_remains_after_sqlite_corruption(self, tmp_log_dir):
        """删除 SQLite 文件后 reindex 仍能恢复全部数据 → 证明 JSONL 是事实源。"""
        log_dir, db_path = tmp_log_dir
        _write_jsonl(log_dir, "query_trace.2026-06-12.jsonl", _make_events("t-recover"))

        from server import bi_index

        bi_index.upsert_turn("t-recover")
        assert db_path.exists()

        # 模拟 SQLite 损毁：直接删
        db_path.unlink()
        assert not db_path.exists()

        stats = bi_index.reindex_from_jsonl()
        assert stats["indexed_traces"] == 1
        with bi_index._connect() as conn:
            row = conn.execute("SELECT trace_id FROM turn_summary").fetchone()
        assert row["trace_id"] == "t-recover"
