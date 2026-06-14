"""v0.5.1 logger 按天轮转 + alerter trace_id 关联测试。

独立文件以避开 ``tests/test_logger_async.py`` autouse fixture 的 patch 干扰。
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from server.logger import (
    _BEIJING_TZ,
    _get_log_file_for_today,
    _iter_log_files,
    _write_event_sync,
    cleanup_old_logs,
    find_trace_events,
    read_traces,
)


class TestGetLogFileForToday:
    def test_filename_contains_today_in_beijing_tz(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        path = _get_log_file_for_today()
        today = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d")
        assert path.parent == tmp_path
        assert path.name == f"query_trace.{today}.jsonl"


class TestWriteEventSyncRotation:
    def test_write_lands_in_today_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        _write_event_sync({"traceId": "rot-1", "phase": "routing_input", "data": {}})
        today_file = _get_log_file_for_today()
        assert today_file.exists()
        lines = today_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["traceId"] == "rot-1"


class TestIterLogFiles:
    def test_iter_aggregates_legacy_and_dated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        (tmp_path / "query_trace.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "query_trace.2025-01-01.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "query_trace.2025-01-02.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "irrelevant.log").write_text("noise", encoding="utf-8")
        names = [p.name for p in _iter_log_files()]
        assert names == [
            "query_trace.jsonl",
            "query_trace.2025-01-01.jsonl",
            "query_trace.2025-01-02.jsonl",
        ]

    def test_iter_returns_empty_when_dir_blank(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        assert _iter_log_files() == []


class TestCrossFileRead:
    def test_find_trace_events_aggregates_across_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        (tmp_path / "query_trace.2025-01-01.jsonl").write_text(
            json.dumps({"traceId": "T1", "phase": "routing_input", "data": {}}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "query_trace.2025-01-02.jsonl").write_text(
            json.dumps({"traceId": "T1", "phase": "final", "data": {"result": "success"}}) + "\n",
            encoding="utf-8",
        )
        events = find_trace_events("T1")
        assert [e["phase"] for e in events] == ["routing_input", "final"]

    def test_read_traces_aggregates_across_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        (tmp_path / "query_trace.2025-01-01.jsonl").write_text(
            json.dumps({"traceId": "A", "phase": "routing_input"}) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "query_trace.2025-01-02.jsonl").write_text(
            json.dumps({"traceId": "B", "phase": "routing_input"}) + "\n",
            encoding="utf-8",
        )
        traces = read_traces(limit=10)
        ids = [t["traceId"] for t in traces]
        assert ids[0] == "B"
        assert ids[1] == "A"


class TestCleanupOldLogs:
    def test_removes_files_older_than_keep_days(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        today = datetime.now(_BEIJING_TZ).date()
        old_date = (today - timedelta(days=45)).strftime("%Y-%m-%d")
        recent_date = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        old_file = tmp_path / f"query_trace.{old_date}.jsonl"
        recent_file = tmp_path / f"query_trace.{recent_date}.jsonl"
        legacy_file = tmp_path / "query_trace.jsonl"
        for p in (old_file, recent_file, legacy_file):
            p.write_text("x", encoding="utf-8")

        result = cleanup_old_logs(keep_days=30)
        assert old_file.name in result["deleted"]
        assert recent_file.name not in result["deleted"]
        assert not old_file.exists()
        assert recent_file.exists()
        assert legacy_file.exists()
        assert result["kept_days"] == 30

    def test_skips_malformed_filenames(self, tmp_path, monkeypatch):
        monkeypatch.setattr("server.logger.LOG_DIR", tmp_path)
        weird = tmp_path / "query_trace.NOT-A-DATE.jsonl"
        weird.write_text("x", encoding="utf-8")
        result = cleanup_old_logs(keep_days=30)
        assert weird.exists()
        assert weird.name not in result["deleted"]


class TestAlerterRecentTraces:
    def test_push_and_consume_returns_fifo(self):
        from server.monitor.alerter import Alerter

        alerter = Alerter()
        alerter.push_failure_trace("t1")
        alerter.push_failure_trace("t2")
        alerter.push_failure_trace("t3")
        assert alerter.consume_recent_failure_traces() == ["t1", "t2", "t3"]
        assert alerter.consume_recent_failure_traces() == []

    def test_push_dedupe_and_skip_unknown(self):
        from server.monitor.alerter import Alerter

        alerter = Alerter()
        alerter.push_failure_trace("dup")
        alerter.push_failure_trace("dup")
        alerter.push_failure_trace("")
        alerter.push_failure_trace("unknown")
        assert alerter.consume_recent_failure_traces() == ["dup"]

    def test_push_caps_at_max(self):
        from server.monitor.alerter import _MAX_RECENT_FAILURE_TRACES, Alerter

        alerter = Alerter()
        for i in range(_MAX_RECENT_FAILURE_TRACES + 3):
            alerter.push_failure_trace(f"t{i}")
        traces = alerter.consume_recent_failure_traces()
        assert len(traces) == _MAX_RECENT_FAILURE_TRACES
        assert "t0" not in traces
        assert f"t{_MAX_RECENT_FAILURE_TRACES + 2}" in traces


class TestAlerterSendRendersTraces:
    def test_send_alert_renders_trace_links_in_body(self, monkeypatch):
        from server.monitor.alerter import Alerter, AlertLevel

        alerter = Alerter()
        alerter._bark_url = "http://example.invalid/bark"
        alerter.push_failure_trace("trace-A")
        alerter.push_failure_trace("trace-B")

        captured: dict[str, str] = {}

        def fake_bark(title, body, group, level):
            captured["body"] = body
            captured["title"] = title
            return True

        monkeypatch.setattr(alerter, "_send_bark_sync", fake_bark)

        ok = asyncio.run(alerter.send_alert(level=AlertLevel.WARNING, title="t", message="base body", alert_key="k1"))
        assert ok is True
        body = captured.get("body", "")
        assert "base body" in body
        assert "trace-A" in body
        assert "trace-B" in body
        assert "/admin/logs?trace_id=trace-A" in body

        alerter._sent_alerts.clear()
        captured.clear()
        ok = asyncio.run(alerter.send_alert(level=AlertLevel.WARNING, title="t2", message="next", alert_key="k2"))
        assert "trace-A" not in captured.get("body", "")
        assert "trace-B" not in captured.get("body", "")

    def test_recovery_does_not_consume_traces(self, monkeypatch):
        from server.monitor.alerter import Alerter, AlertLevel

        alerter = Alerter()
        alerter._bark_url = "http://example.invalid/bark"
        alerter.push_failure_trace("trace-keep")

        monkeypatch.setattr(alerter, "_send_bark_sync", lambda *a, **kw: True)
        ok = asyncio.run(alerter.send_alert(level=AlertLevel.RECOVERY, title="ok", message="recover", alert_key="r1"))
        assert ok is True
        assert alerter.consume_recent_failure_traces() == ["trace-keep"]


_ = pytest
