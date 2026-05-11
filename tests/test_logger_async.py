"""异步日志写入测试。"""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# 需要在导入前 patch LOG_FILE，避免污染真实日志
_tmp_dir = tempfile.mkdtemp()
_tmp_log = Path(_tmp_dir) / "test_trace.jsonl"


@pytest.fixture(autouse=True)
def _patch_log_file():
    """每个测试用独立的临时日志文件。"""
    # 清空文件
    _tmp_log.write_text("")
    with patch("server.logger.LOG_FILE", _tmp_log):
        yield


# ── 导入被测模块（必须在 patch fixture 之后，否则 LOG_FILE 无法被替换） ──
from server.logger import (  # noqa: E402
    _build_trace_data,
    _write_event_sync,
    find_trace,
    find_trace_events,
    log_chat_trace,
    log_chat_trace_async,
    log_trace_event,
    log_trace_event_sync,
    read_trace_summaries,
    read_traces,
)

# 向后兼容别名
_write_trace_sync = _write_event_sync

# 使用 anyio 后端运行异步测试
pytestmark = pytest.mark.anyio


class TestBuildTraceData:
    """_build_trace_data 数据结构测试。"""

    def test_basic_fields(self):
        data = _build_trace_data("t1", "hello", {"intent": "query"}, 5, "reply text")
        assert data["traceId"] == "t1"
        assert data["query"] == "hello"
        assert data["intent"] == {"intent": "query"}
        assert data["results_count"] == 5
        assert data["reply"] == "reply text"
        assert data["level"] == "INFO"
        assert "timestamp" in data
        assert "error" not in data

    def test_error_field(self):
        data = _build_trace_data("t2", "q", {}, 0, "fail", error="boom")
        assert data["level"] == "ERROR"
        assert data["error"] == "boom"

    def test_context_field(self):
        ctx = {"llm_model": "gpt-4o"}
        data = _build_trace_data("t3", "q", {}, 1, "ok", context=ctx)
        assert data["context"]["llm_model"] == "gpt-4o"


class TestWriteTraceSync:
    """_write_trace_sync 同步写入测试。"""

    def test_appends_jsonl(self):
        _write_trace_sync({"traceId": "s1", "msg": "first"})
        _write_trace_sync({"traceId": "s2", "msg": "second"})
        lines = _tmp_log.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["traceId"] == "s1"
        assert json.loads(lines[1])["traceId"] == "s2"


class TestLogChatTraceSync:
    """log_chat_trace 同步版本测试。"""

    def test_writes_valid_jsonl(self):
        log_chat_trace("sync1", "query text", {"k": "v"}, 3, "reply")
        lines = _tmp_log.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["traceId"] == "sync1"
        assert entry["level"] == "INFO"


class TestLogChatTraceAsync:
    """log_chat_trace_async 异步版本测试。"""

    async def test_async_writes_valid_jsonl(self):
        await log_chat_trace_async("a1", "async query", {}, 2, "async reply")
        lines = _tmp_log.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["traceId"] == "a1"
        assert entry["query"] == "async query"

    async def test_async_error_trace(self):
        await log_chat_trace_async("a2", "q", {}, 0, "fail", error="timeout")
        entry = json.loads(_tmp_log.read_text().strip())
        assert entry["level"] == "ERROR"
        assert entry["error"] == "timeout"

    async def test_concurrent_writes_no_data_loss(self):
        """并发写入 50 条日志，验证无数据丢失。"""
        tasks = [log_chat_trace_async(f"c{i}", f"q{i}", {}, i, f"r{i}") for i in range(50)]
        await asyncio.gather(*tasks)
        lines = _tmp_log.read_text().strip().split("\n")
        assert len(lines) == 50
        trace_ids = {json.loads(line)["traceId"] for line in lines}
        assert trace_ids == {f"c{i}" for i in range(50)}


class TestReadTracesCompat:
    """验证 read_traces 能正确读取新格式日志。"""

    def test_read_after_async_write(self):
        # 先同步写入几条（模拟异步写入后的文件状态）
        log_chat_trace("r1", "q1", {}, 1, "reply1")
        log_chat_trace("r2", "q2", {}, 2, "reply2")
        traces = read_traces(limit=10)
        assert len(traces) == 2
        # 倒序：最新在前
        assert traces[0]["traceId"] == "r2"
        assert traces[1]["traceId"] == "r1"


# ============================================================
# 多阶段事件日志（新模式）测试
# ============================================================


class TestLogTraceEventBasic:
    """验证单阶段事件写入和读取。"""

    def test_sync_event_write(self):
        log_trace_event_sync("ev1", "routing_input", {"query": "hello", "skill_count": 5})
        lines = _tmp_log.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["traceId"] == "ev1"
        assert entry["phase"] == "routing_input"
        assert entry["data"]["query"] == "hello"
        assert entry["data"]["skill_count"] == 5
        assert "timestamp" in entry
        assert "error" not in entry

    def test_event_with_error(self):
        log_trace_event_sync("ev2", "final", {"result": "error"}, error="timeout")
        entry = json.loads(_tmp_log.read_text().strip())
        assert entry["level"] == "ERROR"
        assert entry["error"] == "timeout"
        assert entry["phase"] == "final"

    async def test_async_event_write(self):
        await log_trace_event("ev3", "execution", {"total_found": 10})
        lines = _tmp_log.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["traceId"] == "ev3"
        assert entry["phase"] == "execution"


class TestFindTraceEventsAggregation:
    """验证多阶段事件按 traceId 聚合。"""

    def test_aggregation(self):
        # 写入同一 traceId 的多个阶段事件
        log_trace_event_sync("agg1", "routing_input", {"query": "test"})
        log_trace_event_sync("agg1", "routing_output", {"skill_calls": []})
        log_trace_event_sync("agg1", "execution", {"total_found": 3})
        log_trace_event_sync("other", "routing_input", {"query": "noise"})
        log_trace_event_sync("agg1", "final", {"total_time_ms": 100})

        events = find_trace_events("agg1")
        assert len(events) == 4
        phases = [e["phase"] for e in events]
        assert phases == ["routing_input", "routing_output", "execution", "final"]

    def test_no_match(self):
        log_trace_event_sync("x1", "routing_input", {})
        events = find_trace_events("nonexistent")
        assert events == []


class TestTraceEventOrdering:
    """验证事件按时间顺序返回。"""

    def test_ordering(self):
        phases = ["routing_input", "routing_output", "execution", "context_build", "generation_output", "final"]
        for phase in phases:
            log_trace_event_sync("ord1", phase, {"step": phase})

        events = find_trace_events("ord1")
        assert len(events) == 6
        returned_phases = [e["phase"] for e in events]
        assert returned_phases == phases


class TestBackwardCompatibility:
    """验证旧模式 log_chat_trace / find_trace 仍正常工作。"""

    def test_old_mode_write_and_find(self):
        log_chat_trace("bc1", "old query", {"intent": "test"}, 5, "old reply")
        result = find_trace("bc1")
        assert result is not None
        assert result["traceId"] == "bc1"
        assert result.get("query") == "old query"

    def test_new_mode_find_trace_aggregated(self):
        """find_trace 对多阶段事件返回聚合视图。"""
        log_trace_event_sync("bc2", "routing_input", {"query": "new query"})
        log_trace_event_sync("bc2", "routing_output", {"skill_calls": [{"skill_name": "s1"}]})
        log_trace_event_sync("bc2", "execution", {"total_found": 10})
        log_trace_event_sync("bc2", "generation_output", {"reply_preview": "Found 10"})
        log_trace_event_sync("bc2", "final", {"total_time_ms": 200})

        result = find_trace("bc2")
        assert result is not None
        assert result["traceId"] == "bc2"
        assert "phases" in result
        assert len(result["phases"]) == 5
        assert result["query"] == "new query"
        assert result["results_count"] == 10

    def test_mixed_old_and_new(self):
        """混合新旧模式数据，find_trace 正确区分。"""
        # 旧模式
        log_chat_trace("mix1", "old", {}, 1, "reply")
        # 新模式
        log_trace_event_sync("mix2", "routing_input", {"query": "new"})
        log_trace_event_sync("mix2", "final", {"total_time_ms": 50})

        old_result = find_trace("mix1")
        assert old_result is not None
        assert "phases" not in old_result  # 旧模式无 phases
        assert old_result["query"] == "old"

        new_result = find_trace("mix2")
        assert new_result is not None
        assert "phases" in new_result
        assert len(new_result["phases"]) == 2


class TestModeAndTokensInSummary:
    """验证摘要和 find_trace 中包含 mode 和 total_tokens 字段。"""

    def test_summary_includes_mode_and_tokens(self):
        """read_trace_summaries 应返回 mode 和 total_tokens。"""
        log_trace_event_sync("mt1", "routing_input", {"query": "test mode", "mode": "oneshot_llm"})
        log_trace_event_sync(
            "mt1",
            "final",
            {"total_time_ms": 150, "result": "success", "mode": "oneshot", "total_tokens": 1234},
        )

        result = read_trace_summaries(limit=100)
        items = result["items"]
        mt1 = next((s for s in items if s["traceId"] == "mt1"), None)
        assert mt1 is not None
        assert mt1["mode"] == "oneshot"
        assert mt1["total_tokens"] == 1234

    def test_find_trace_includes_mode_and_tokens(self):
        """find_trace 聚合视图应包含 mode 和 total_tokens。"""
        log_trace_event_sync("mt2", "routing_input", {"query": "agent test", "mode": "oneshot_llm"})
        log_trace_event_sync(
            "mt2",
            "final",
            {"total_time_ms": 300, "result": "agent_fallback", "mode": "agent_fallback", "total_tokens": 5678},
        )

        result = find_trace("mt2")
        assert result is not None
        assert result["mode"] == "agent_fallback"
        assert result["total_tokens"] == 5678
