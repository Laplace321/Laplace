"""中断 + 恢复路径测试（Task 4 Batch B — ADR-028）。

聚焦三层契约：
1. ``clarify_node`` 的 ``_maybe_save_pending`` 在配置 SessionStore + session_id 时把
   PipelineState 写入 pending checkpoint，且过滤不可 pickle 的 extras 句柄
2. ``clarify_node`` 不持有 SessionStore 时不污染主流程，state.query 不应有 pending 标记
3. ``resume_skill_mode`` 在 pending 缺失时优雅返回，pending 命中时分发到 handle_skill_mode
"""

from __future__ import annotations

import pickle
from unittest.mock import AsyncMock, patch

import pytest

from server.graph.checkpointer import InMemoryCheckpointer
from server.graph.session import SessionStore
from server.graph.state import PipelineState
from server.nodes.clarify import clarify_node


def _make_session_store() -> SessionStore:
    return SessionStore(InMemoryCheckpointer(ttl_seconds=60))


# ────────────────────────────────────────────────────────────
# clarify_node — routing clarification + save_pending
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_routing_saves_pending_and_marks_query():
    store = _make_session_store()
    state = PipelineState(
        user_message="查一下伊织",
        trace_id="t-clarify-route",
        session_id="sid-cl-1",
    )
    state.extras["bail_out"] = "clarification"
    state.extras["routing_result"] = {
        "clarification": {
            "question": "你说的是哪个伊织？",
            "options": [{"label": "宫本伊织"}, {"label": "宇佐美伊织"}],
            "ambiguous_field": "name",
        }
    }
    state.extras["session_store"] = store

    out = await clarify_node(state)

    # query 应包含 clarification 数据 + pending 标记
    assert out.query["mode"] == "clarification"
    assert out.query["clarification"]["question"] == "你说的是哪个伊织？"
    assert out.query["pending"] is True
    assert out.query["session_id"] == "sid-cl-1"

    # SessionStore 应有对应 pending 记录
    pending = store.load_pending("sid-cl-1")
    assert pending is not None
    assert isinstance(pending, PipelineState)
    assert pending.user_message == "查一下伊织"
    assert pending.session_id == "sid-cl-1"


@pytest.mark.asyncio
async def test_clarify_pending_strips_unpicklable_extras():
    """save_pending 必须清掉 session_store / executor_result / prev_turn 等不可 pickle 的字段。"""

    class Unpicklable:
        def __reduce__(self):
            raise TypeError("intentionally unpicklable")

    store = _make_session_store()
    state = PipelineState(
        user_message="x",
        trace_id="t-clarify-pickle",
        session_id="sid-cl-2",
    )
    state.extras["bail_out"] = "clarification"
    state.extras["routing_result"] = {"clarification": {"question": "?", "options": []}}
    state.extras["session_store"] = store
    state.extras["executor_result"] = Unpicklable()
    state.extras["prev_turn"] = Unpicklable()
    # 一个普通可 pickle 的字段：应被保留
    state.extras["custom_marker"] = "keep_me"

    out = await clarify_node(state)

    pending = store.load_pending("sid-cl-2")
    assert pending is not None
    # 三个不可 pickle 字段必须被剥离
    assert "session_store" not in pending.extras
    assert "executor_result" not in pending.extras
    assert "prev_turn" not in pending.extras
    # 普通字段保留
    assert pending.extras.get("custom_marker") == "keep_me"
    # 持久化层确实可以再次 pickle（保险）
    pickle.dumps(pending)
    # query.pending 应仍被打标
    assert out.query["pending"] is True


@pytest.mark.asyncio
async def test_clarify_without_session_id_does_not_save_pending():
    """session_id 为空 → 不写 pending，query 也不应有 pending 标记。"""
    store = _make_session_store()
    state = PipelineState(
        user_message="查询伊织",
        trace_id="t-clarify-nosid",
        session_id="",
    )
    state.extras["bail_out"] = "clarification"
    state.extras["routing_result"] = {"clarification": {"question": "?", "options": []}}
    state.extras["session_store"] = store

    out = await clarify_node(state)

    # 没有 session_id 不应写 pending
    assert store.load_pending("") is None
    assert "pending" not in out.query


@pytest.mark.asyncio
async def test_clarify_without_session_store_does_not_break():
    """没有 session_store 也应正常完成 clarify，仅退化为单轮行为。"""
    state = PipelineState(
        user_message="查询伊织",
        trace_id="t-clarify-nostore",
        session_id="sid-cl-3",
    )
    state.extras["bail_out"] = "clarification"
    state.extras["routing_result"] = {"clarification": {"question": "?", "options": []}}

    out = await clarify_node(state)

    assert out.query["mode"] == "clarification"
    assert "pending" not in out.query


@pytest.mark.asyncio
async def test_clarify_save_pending_failure_does_not_break_main_flow():
    """SessionStore.save_pending 抛错时主流程仍能正常返回 clarification。"""

    class FailingStore:
        def save_pending(self, _sid, _state):
            raise RuntimeError("disk full")

    state = PipelineState(
        user_message="query",
        trace_id="t-clarify-fail",
        session_id="sid-cl-4",
    )
    state.extras["bail_out"] = "clarification"
    state.extras["routing_result"] = {"clarification": {"question": "?", "options": []}}
    state.extras["session_store"] = FailingStore()

    out = await clarify_node(state)

    # 主流程不受影响：query 仍被填好
    assert out.query["mode"] == "clarification"
    # 保存失败 → query 不应被打 pending 标记（避免前端误以为已 pending）
    assert "pending" not in out.query


# ────────────────────────────────────────────────────────────
# clarify_node — execution clarification 路径
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_execution_clarification_saves_pending():
    from server.skills.executor import ExecutionResult

    store = _make_session_store()
    fake_clarification = {
        "type": "ambiguous_servant",
        "question": "找到多位候选，请选择",
        "options": [{"label": "梅林"}, {"label": "梅露辛"}],
        "ambiguous_field": "name",
    }
    fake_result = ExecutionResult(
        servants=[],
        total_found=0,
        response_skill=None,
        is_fallback=False,
        accepted_skills=[],
        rejected_skills=[],
        execution_time_ms=1.0,
        clarification=fake_clarification,
        custom_context=None,
    )

    state = PipelineState(
        user_message="查梅林",
        trace_id="t-clarify-exec",
        session_id="sid-cl-5",
    )
    state.extras["bail_out"] = "execution_clarification"
    state.extras["executor_result"] = fake_result
    state.extras["session_store"] = store

    out = await clarify_node(state)

    assert out.query["mode"] == "clarification"
    assert out.query["source"] == "execution"
    assert out.query["pending"] is True

    pending = store.load_pending("sid-cl-5")
    assert pending is not None
    # executor_result 不应进入 pending（不可 pickle 的复杂对象）
    assert "executor_result" not in pending.extras


# ────────────────────────────────────────────────────────────
# resume_skill_mode — pending 缺失 / 命中
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_skill_mode_no_pending_returns_friendly_error():
    from server.pipeline import resume_skill_mode

    store = _make_session_store()  # 空 store，不存在 pending

    with patch("server.pipeline._get_session_store", return_value=store):
        resp = await resume_skill_mode(
            session_id="missing-sid",
            supplement_message="宫本伊织",
            trace_id="t-resume-miss",
        )

    # 应返回友好错误响应，不抛异常
    assert "对话已超时" in resp.reply or "不存在" in resp.reply
    assert resp.query.get("error") == "no_pending"
    assert resp.model == "error"


@pytest.mark.asyncio
async def test_resume_skill_mode_with_pending_dispatches_to_handle_skill_mode():
    """pending 命中时应：清 pending → 调 handle_skill_mode（原 message 当 confirmation_context）。"""
    from server.pipeline import ChatResponse, resume_skill_mode

    store = _make_session_store()
    # 模拟 clarify 时存下的 pending state
    pending_state = PipelineState(
        user_message="查一下伊织",  # 用户原始模糊提问
        trace_id="t-original",
        session_id="sid-resume",
    )
    store.save_pending("sid-resume", pending_state)

    captured: dict[str, object] = {}

    async def fake_handle_skill_mode(**kwargs):
        captured.update(kwargs)
        return ChatResponse(
            reply="OK",
            servants=[],
            count=0,
            query={},
            model="fake",
            traceId=kwargs["trace_id"],
        )

    with (
        patch("server.pipeline._get_session_store", return_value=store),
        patch("server.pipeline.handle_skill_mode", side_effect=fake_handle_skill_mode),
    ):
        resp = await resume_skill_mode(
            session_id="sid-resume",
            supplement_message="宫本伊织",  # 用户补充答复
            trace_id="t-resume-hit",
            client_ip="1.2.3.4",
        )

    # 应清掉 pending 避免重复消费
    assert store.load_pending("sid-resume") is None

    # 用户答复作为 user_message；原 message 作为 confirmation_context；session_id 透传
    assert captured["user_message"] == "宫本伊织"
    assert captured["confirmation_context"] == "查一下伊织"
    assert captured["session_id"] == "sid-resume"
    assert captured["trace_id"] == "t-resume-hit"
    assert captured["client_ip"] == "1.2.3.4"
    assert resp.reply == "OK"


@pytest.mark.asyncio
async def test_resume_skill_mode_with_pending_no_original_message():
    """pending 中 user_message 为空时 → confirmation_context 应传 None 而非空串。"""
    from server.pipeline import ChatResponse, resume_skill_mode

    store = _make_session_store()
    pending_state = PipelineState(
        user_message="",  # 异常情况：空 user_message
        trace_id="t",
        session_id="sid-resume-empty",
    )
    store.save_pending("sid-resume-empty", pending_state)

    fake_handler = AsyncMock(
        return_value=ChatResponse(reply="x", servants=[], count=0, query={}, model="m", traceId="t")
    )

    with (
        patch("server.pipeline._get_session_store", return_value=store),
        patch("server.pipeline.handle_skill_mode", new=fake_handler),
    ):
        await resume_skill_mode(
            session_id="sid-resume-empty",
            supplement_message="补充",
            trace_id="t-resume-empty",
        )

    kwargs = fake_handler.await_args.kwargs
    assert kwargs["confirmation_context"] is None
