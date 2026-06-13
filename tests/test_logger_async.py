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
    PHASES,
    Phase,
    _build_trace_data,
    _write_event_sync,
    bind_trace_id,
    current_trace_id,
    find_trace,
    find_trace_events,
    get_trace_id,
    log_chat_trace,
    log_chat_trace_async,
    log_trace_event,
    log_trace_event_sync,
    read_trace_summaries,
    read_traces,
    reset_trace_id,
    validate_phase,
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


# ============================================================
# v0.5.1 全链路埋点改造：ContextVar / Phase / with_trace
# ============================================================


class TestPhaseConstants:
    """Phase 常量类与 validate_phase 校验。"""

    def test_phase_constants_in_phases_set(self):
        assert Phase.ROUTING_INPUT in PHASES
        assert Phase.FINAL in PHASES
        assert Phase.CLASSIFIER_OUTPUT in PHASES
        assert Phase.GENERATION_OUTPUT in PHASES

    def test_validate_phase_accepts_known(self):
        assert validate_phase(Phase.ROUTING_INPUT) is True
        assert validate_phase(Phase.FINAL) is True

    def test_validate_phase_accepts_node_decorator_suffixes(self):
        # 装饰器自动产生的 {name}_input/_output/_error 视为合法
        assert validate_phase("classifier_output_input") is True
        assert validate_phase("classifier_output_output") is True
        assert validate_phase("classifier_output_error") is True

    def test_validate_phase_rejects_unknown(self):
        assert validate_phase("totally_unknown_phase") is False
        assert validate_phase("") is False


class TestContextVarPropagation:
    """trace_id ContextVar 在协程间自动传播。"""

    def test_get_trace_id_default_unknown(self):
        # 未绑定时返回 "unknown"
        assert get_trace_id() == "unknown"

    def test_bind_and_reset(self):
        token = bind_trace_id("ctx-001")
        try:
            assert get_trace_id() == "ctx-001"
        finally:
            reset_trace_id(token)
        assert get_trace_id() == "unknown"

    async def test_log_trace_event_picks_contextvar(self):
        """log_trace_event(trace_id=None, ...) 自动从 ContextVar 取 trace_id。"""
        token = bind_trace_id("ctx-async-1")
        try:
            await log_trace_event(None, Phase.ROUTING_INPUT, {"query": "ctx test"})
        finally:
            reset_trace_id(token)
        events = find_trace_events("ctx-async-1")
        assert len(events) == 1
        assert events[0]["phase"] == Phase.ROUTING_INPUT
        assert events[0]["data"]["query"] == "ctx test"

    def test_log_trace_event_sync_picks_contextvar(self):
        token = bind_trace_id("ctx-sync-1")
        try:
            log_trace_event_sync(None, Phase.FINAL, {"result": "success"})
        finally:
            reset_trace_id(token)
        events = find_trace_events("ctx-sync-1")
        assert len(events) == 1
        assert events[0]["data"]["result"] == "success"

    def test_explicit_trace_id_overrides_contextvar(self):
        """显式传入的 trace_id 优先于 ContextVar。"""
        token = bind_trace_id("ctx-bg")
        try:
            log_trace_event_sync("explicit-001", Phase.FINAL, {})
        finally:
            reset_trace_id(token)
        assert find_trace_events("explicit-001")
        assert not find_trace_events("ctx-bg")

    async def test_concurrent_contexts_isolated(self):
        """并发协程的 ContextVar 相互隔离。"""

        async def task(tid: str):
            token = bind_trace_id(tid)
            try:
                await asyncio.sleep(0)  # 让出，验证 ContextVar 跨切片不串
                await log_trace_event(None, Phase.ROUTING_INPUT, {"q": tid})
            finally:
                reset_trace_id(token)

        await asyncio.gather(*(task(f"iso-{i}") for i in range(10)))
        for i in range(10):
            evs = find_trace_events(f"iso-{i}")
            assert len(evs) == 1
            assert evs[0]["data"]["q"] == f"iso-{i}"


class TestWithTraceDecorator:
    """server.graph.decorators.with_trace 集成测试。"""

    async def test_with_trace_writes_input_and_output(self):
        from dataclasses import dataclass, field

        from server.graph.decorators import with_trace

        @dataclass
        class FakeState:
            trace_id: str = "wt-001"
            user_message: str = "hi"
            turn_type: str = "MAJOR"
            session_id: str = "sess-1"
            reply: str = ""
            count: int = 0
            metric_labels: dict = field(default_factory=dict)

        @with_trace(Phase.CLASSIFIER_OUTPUT)
        async def fake_node(state: FakeState) -> FakeState:
            state.reply = "world"
            state.count = 5
            state.metric_labels["pipeline"] = "A"
            return state

        result = await fake_node(FakeState())
        assert result.reply == "world"

        events = find_trace_events("wt-001")
        phases = [e["phase"] for e in events]
        assert phases == [
            f"{Phase.CLASSIFIER_OUTPUT}_input",
            f"{Phase.CLASSIFIER_OUTPUT}_output",
        ]
        in_data = events[0]["data"]
        assert in_data["node_name"] == "fake_node"
        assert in_data["user_message_preview"] == "hi"
        assert in_data["turn_type"] == "MAJOR"

        out_data = events[1]["data"]
        assert out_data["result"] == "success"
        assert out_data["latency_ms"] >= 0
        assert out_data["metric_labels"]["pipeline"] == "A"
        assert out_data["reply_preview"] == "world"
        assert out_data["count"] == 5

    async def test_with_trace_records_error_event(self):
        from dataclasses import dataclass

        from server.graph.decorators import with_trace

        @dataclass
        class FakeState:
            trace_id: str = "wt-err"
            user_message: str = ""
            turn_type: str = ""
            session_id: str = ""

        @with_trace(Phase.EXECUTION)
        async def boom_node(state: FakeState) -> FakeState:
            raise ValueError("synthetic-failure")

        with pytest.raises(ValueError, match="synthetic-failure"):
            await boom_node(FakeState())

        events = find_trace_events("wt-err")
        phases = [e["phase"] for e in events]
        assert phases == [
            f"{Phase.EXECUTION}_input",
            f"{Phase.EXECUTION}_error",
        ]
        err_event = events[1]
        assert err_event["level"] == "ERROR"
        assert "synthetic-failure" in err_event["error"]
        assert err_event["data"]["error_type"] == "ValueError"
        assert err_event["data"]["latency_ms"] >= 0

    async def test_with_trace_uses_contextvar_when_state_missing(self):
        """state.trace_id 为空时，从 ContextVar 取 trace_id。"""
        from dataclasses import dataclass

        from server.graph.decorators import with_trace

        @dataclass
        class FakeState:
            trace_id: str = ""
            user_message: str = ""
            turn_type: str = ""
            session_id: str = ""

        @with_trace(Phase.ROUTING_OUTPUT)
        async def node(state: FakeState) -> FakeState:
            return state

        token = bind_trace_id("ctx-decorator")
        try:
            await node(FakeState())
        finally:
            reset_trace_id(token)

        events = find_trace_events("ctx-decorator")
        assert len(events) == 2  # input + output
        # current_trace_id 仅作为 import 引用使用，避免 ruff 未使用告警
        assert current_trace_id is not None
