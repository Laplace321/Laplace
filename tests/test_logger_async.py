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
    _write_trace_sync,
    log_chat_trace,
    log_chat_trace_async,
    read_traces,
)

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
