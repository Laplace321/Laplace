"""StateGraph 图引擎 API 单测（Task 1）。

测试目标：
- 节点注册（异常路径）
- 静态边 / 条件边
- run() 推进逻辑
- 死循环保护
- resume() 基础路径
- 流式节点事件收集
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from server.graph import END, StateGraph
from server.graph.engine import GraphConfigError


@dataclass
class _MiniState:
    counter: int = 0
    path: list[str] = field(default_factory=list)
    branch: str = ""
    pending_events: list[dict] = field(default_factory=list)


# ──────────────────────── 注册校验 ────────────────────────


def test_add_node_rejects_non_async():
    g = StateGraph()

    def sync_fn(s):
        return s

    with pytest.raises(GraphConfigError, match="async"):
        g.add_node("bad", sync_fn)  # type: ignore[arg-type]


def test_add_node_rejects_duplicate():
    g = StateGraph()

    async def n(s):
        return s

    g.add_node("a", n)
    with pytest.raises(GraphConfigError, match="已注册"):
        g.add_node("a", n)


def test_add_node_rejects_end_keyword():
    g = StateGraph()

    async def n(s):
        return s

    with pytest.raises(GraphConfigError, match="保留字"):
        g.add_node(END, n)


def test_add_edge_requires_both_nodes():
    g = StateGraph()

    async def n(s):
        return s

    g.add_node("a", n)
    with pytest.raises(GraphConfigError, match="未注册"):
        g.add_edge("a", "b")


def test_add_edge_to_END_is_allowed():
    g = StateGraph()

    async def n(s):
        return s

    g.add_node("a", n)
    g.add_edge("a", END)  # 不应抛异常


def test_static_and_conditional_edge_are_mutually_exclusive():
    g = StateGraph()

    async def a(s):
        return s

    async def b(s):
        return s

    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge("a", "b")
    with pytest.raises(GraphConfigError, match="已有静态边"):
        g.add_conditional_edge("a", lambda s: "b")


def test_set_entry_requires_registered_node():
    g = StateGraph()
    with pytest.raises(GraphConfigError, match="未注册"):
        g.set_entry("nope")


# ──────────────────────── run() 推进 ────────────────────────


@pytest.mark.asyncio
async def test_run_executes_static_chain():
    g = StateGraph()

    async def step1(s):
        s.counter += 1
        s.path.append("step1")
        return s

    async def step2(s):
        s.counter += 10
        s.path.append("step2")
        return s

    g.add_node("step1", step1)
    g.add_node("step2", step2)
    g.set_entry("step1")
    g.add_edge("step1", "step2")
    g.add_edge("step2", END)

    state = _MiniState()
    final = await g.run(state)
    assert final.counter == 11
    assert final.path == ["step1", "step2"]


@pytest.mark.asyncio
async def test_run_executes_conditional_branch():
    g = StateGraph()

    async def classify(s):
        s.path.append("classify")
        return s

    async def left(s):
        s.path.append("left")
        return s

    async def right(s):
        s.path.append("right")
        return s

    g.add_node("classify", classify)
    g.add_node("left", left)
    g.add_node("right", right)
    g.set_entry("classify")
    g.add_conditional_edge("classify", lambda s: s.branch)
    g.add_edge("left", END)
    g.add_edge("right", END)

    s_left = _MiniState(branch="left")
    s_right = _MiniState(branch="right")
    assert (await g.run(s_left)).path == ["classify", "left"]
    assert (await g.run(s_right)).path == ["classify", "right"]


@pytest.mark.asyncio
async def test_conditional_edge_to_END():
    g = StateGraph()

    async def n(s):
        s.path.append("n")
        return s

    g.add_node("n", n)
    g.set_entry("n")
    g.add_conditional_edge("n", lambda s: END)

    final = await g.run(_MiniState())
    assert final.path == ["n"]


@pytest.mark.asyncio
async def test_conditional_edge_to_unknown_raises():
    g = StateGraph()

    async def n(s):
        return s

    g.add_node("n", n)
    g.set_entry("n")
    g.add_conditional_edge("n", lambda s: "ghost")

    with pytest.raises(GraphConfigError, match="未注册"):
        await g.run(_MiniState())


@pytest.mark.asyncio
async def test_run_without_entry_raises():
    g = StateGraph()
    with pytest.raises(GraphConfigError, match="entry"):
        await g.run(_MiniState())


@pytest.mark.asyncio
async def test_node_returning_none_keeps_state():
    g = StateGraph()

    async def n(s):
        s.counter = 99
        return None  # 节点忘记返回

    g.add_node("n", n)
    g.set_entry("n")
    g.add_edge("n", END)

    s = _MiniState()
    final = await g.run(s)
    assert final is s
    assert final.counter == 99  # 原地修改仍生效


# ──────────────────────── 死循环保护 ────────────────────────


@pytest.mark.asyncio
async def test_max_hops_protects_against_infinite_loop():
    g = StateGraph()

    async def a(s):
        s.counter += 1
        return s

    async def b(s):
        s.counter += 1
        return s

    g.add_node("a", a)
    g.add_node("b", b)
    g.set_entry("a")
    g.add_edge("a", "b")
    g.add_conditional_edge("b", lambda s: "a")  # 永远跳回 a

    g.MAX_HOPS = 5
    with pytest.raises(RuntimeError, match="最大跳转次数"):
        await g.run(_MiniState())


# ──────────────────────── resume() ────────────────────────


@pytest.mark.asyncio
async def test_resume_starts_from_specified_node():
    g = StateGraph()

    async def step1(s):
        s.path.append("step1")
        return s

    async def step2(s):
        s.path.append("step2")
        return s

    g.add_node("step1", step1)
    g.add_node("step2", step2)
    g.set_entry("step1")
    g.add_edge("step1", "step2")
    g.add_edge("step2", END)

    state = _MiniState(path=["external"])
    final = await g.resume(state, from_node="step2")
    # 跳过 step1，仅执行 step2
    assert final.path == ["external", "step2"]


@pytest.mark.asyncio
async def test_resume_loads_state_from_checkpointer():
    """state=None + checkpointer + session_id 时引擎从 checkpointer 加载。"""
    from server.graph.checkpointer import InMemoryCheckpointer

    g = StateGraph()

    async def step2(s):
        s.path.append("step2")
        return s

    async def step3(s):
        s.path.append("step3")
        return s

    g.add_node("step2", step2)
    g.add_node("step3", step3)
    g.set_entry("step2")
    g.add_edge("step2", "step3")
    g.add_edge("step3", END)

    cp = InMemoryCheckpointer()
    saved_state = _MiniState(path=["from_ckpt"])
    cp.save("sid-1", saved_state)

    final = await g.resume(None, from_node="step2", checkpointer=cp, session_id="sid-1")
    assert final.path == ["from_ckpt", "step2", "step3"]


@pytest.mark.asyncio
async def test_resume_state_none_without_checkpointer_raises():
    g = StateGraph()

    async def step(s):
        return s

    g.add_node("step", step)
    g.set_entry("step")
    g.add_edge("step", END)

    with pytest.raises(ValueError, match="checkpointer 与 session_id"):
        await g.resume(None, from_node="step")


@pytest.mark.asyncio
async def test_resume_state_none_with_missing_session_raises():
    from server.graph.checkpointer import InMemoryCheckpointer

    g = StateGraph()

    async def step(s):
        return s

    g.add_node("step", step)
    g.set_entry("step")
    g.add_edge("step", END)

    cp = InMemoryCheckpointer()
    with pytest.raises(LookupError, match="未找到"):
        await g.resume(None, from_node="step", checkpointer=cp, session_id="missing")


# ──────────────────────── 流式节点 ────────────────────────


@pytest.mark.asyncio
async def test_stream_node_collects_events_in_pending():
    g = StateGraph()

    async def streaming_node(state):
        yield {"type": "thinking", "msg": "hello"}
        state.counter = 7
        yield state
        yield {"type": "delta", "text": "world"}

    g.add_stream_node("streamer", streaming_node)
    g.set_entry("streamer")
    g.add_edge("streamer", END)

    final = await g.run(_MiniState())
    # state 被流式节点更新
    assert final.counter == 7
    # 流式节点的 dict 事件被收集到 pending_events
    assert {"type": "thinking", "msg": "hello"} in final.pending_events
    assert {"type": "delta", "text": "world"} in final.pending_events


@pytest.mark.asyncio
async def test_run_stream_yields_pending_events():
    g = StateGraph()

    async def streaming_node(state):
        yield {"type": "thinking", "msg": "step-1"}
        yield state
        yield {"type": "delta", "text": "abc"}

    g.add_stream_node("streamer", streaming_node)
    g.set_entry("streamer")
    g.add_edge("streamer", END)

    events = []
    async for e in g.run_stream(_MiniState()):
        events.append(e)
    assert events == [
        {"type": "thinking", "msg": "step-1"},
        {"type": "delta", "text": "abc"},
    ]


# ──────────────────────── 装饰器集成 ────────────────────────


@pytest.mark.asyncio
async def test_with_retry_recovers_after_failure():
    from server.graph.decorators import with_retry

    attempts = {"count": 0}

    @with_retry(times=2)
    async def flaky(state):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("transient")
        state.counter = 42
        return state

    g = StateGraph()
    g.add_node("flaky", flaky)
    g.set_entry("flaky")
    g.add_edge("flaky", END)

    final = await g.run(_MiniState())
    assert final.counter == 42
    assert attempts["count"] == 3


@pytest.mark.asyncio
async def test_with_retry_raises_after_exhaustion():
    from server.graph.decorators import with_retry

    @with_retry(times=1)
    async def always_fail(state):
        raise RuntimeError("persistent")

    g = StateGraph()
    g.add_node("bad", always_fail)
    g.set_entry("bad")
    g.add_edge("bad", END)

    with pytest.raises(RuntimeError, match="persistent"):
        await g.run(_MiniState())
