"""StateGraph — 自研轻量化 DAG 图引擎（ADR-028）。

设计目标：
- ~200 行实现，零外部依赖
- 节点 = async 纯函数 State -> State
- 边 = 静态字符串 或 router(State) -> str 条件函数
- run(): 同步执行，按 entry → next → ... → END 推进
- run_stream(): 流式执行（async generator 节点支持，Task 5 完善）
- resume(): 从 checkpoint 恢复（Task 4 启用）

Task 4 Batch A 扩展 resume：可选传入 ``checkpointer`` + ``session_id``，
引擎自动 load state 再从 ``from_node`` 推进；业务侧（如 supplement 注入）由调用方
在传入 state.extras 时完成，避免引擎层耦合 SessionStore 语义。
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型检查需要，运行时不引入循环依赖
    from server.graph.checkpointer import Checkpointer

# ────────────────────────────────────────────────────────────
# 类型别名
# ────────────────────────────────────────────────────────────

NodeFn = Callable[[Any], Awaitable[Any]]
StreamNodeFn = Callable[[Any], AsyncGenerator[dict, None]]
EdgeRouter = Callable[[Any], str]

#: 终止节点哨兵。条件边或 ``add_edge`` 返回此值表示图执行结束。
END = "__END__"


class GraphConfigError(ValueError):
    """图配置错误（节点未注册、重复注册、缺失 entry 等）。"""


class StateGraph:
    """声明式 DAG 图引擎。

    Example::

        graph = StateGraph()
        graph.add_node("classify", classify_node)
        graph.add_node("atlas", atlas_node)
        graph.add_node("guide", guide_node)
        graph.set_entry("classify")
        graph.add_conditional_edge("classify", lambda s: s.classified_pipeline.lower())
        graph.add_edge("atlas", END)
        graph.add_edge("guide", END)

        final_state = await graph.run(initial_state)
    """

    # 防止图无限循环的硬上限（节点跳转次数）
    MAX_HOPS = 50

    def __init__(self) -> None:
        self._nodes: dict[str, NodeFn] = {}
        self._stream_nodes: dict[str, StreamNodeFn] = {}
        self._static_edges: dict[str, str] = {}
        self._cond_edges: dict[str, EdgeRouter] = {}
        self._entry: str | None = None

    # ── 注册 API ──────────────────────────────────────────────

    def add_node(self, name: str, fn: NodeFn) -> None:
        """注册一个普通异步节点。

        节点签名必须是 ``async def fn(state) -> state``。
        节点应原地修改 state 并返回同一实例（或返回新实例，取决于 state 设计）。
        """
        if name == END:
            raise GraphConfigError(f"节点名不能使用保留字 {END!r}")
        if name in self._nodes or name in self._stream_nodes:
            raise GraphConfigError(f"节点 {name!r} 已注册")
        if not inspect.iscoroutinefunction(fn):
            raise GraphConfigError(f"节点 {name!r} 必须是 async 函数")
        self._nodes[name] = fn

    def add_stream_node(self, name: str, fn: StreamNodeFn) -> None:
        """注册一个流式节点（async generator，yield SSE 事件）。

        Task 5 启用；本期注册后 run() 会按普通节点处理（消费 generator 但不转发事件）。
        """
        if name == END:
            raise GraphConfigError(f"节点名不能使用保留字 {END!r}")
        if name in self._nodes or name in self._stream_nodes:
            raise GraphConfigError(f"节点 {name!r} 已注册")
        if not inspect.isasyncgenfunction(fn):
            raise GraphConfigError(f"流式节点 {name!r} 必须是 async generator 函数")
        self._stream_nodes[name] = fn

    def add_edge(self, from_node: str, to_node: str) -> None:
        """注册一条静态边：from_node 完成后无条件跳转到 to_node。"""
        self._validate_node_exists(from_node, where="add_edge.from_node")
        if to_node != END:
            self._validate_node_exists(to_node, where="add_edge.to_node")
        if from_node in self._cond_edges:
            raise GraphConfigError(f"节点 {from_node!r} 已有条件边，不能再添加静态边")
        if from_node in self._static_edges:
            raise GraphConfigError(f"节点 {from_node!r} 已有静态边 -> {self._static_edges[from_node]!r}")
        self._static_edges[from_node] = to_node

    def add_conditional_edge(self, from_node: str, router: EdgeRouter) -> None:
        """注册一条条件边：router(state) 返回下一节点名（或 END）。"""
        self._validate_node_exists(from_node, where="add_conditional_edge.from_node")
        if from_node in self._static_edges:
            raise GraphConfigError(f"节点 {from_node!r} 已有静态边，不能再添加条件边")
        if from_node in self._cond_edges:
            raise GraphConfigError(f"节点 {from_node!r} 已有条件边")
        self._cond_edges[from_node] = router

    def set_entry(self, name: str) -> None:
        """设置图的入口节点。"""
        self._validate_node_exists(name, where="set_entry")
        self._entry = name

    # ── 执行 API ──────────────────────────────────────────────

    async def run(self, state: Any) -> Any:
        """同步执行图，从 entry 推进到 END，返回最终 state。

        遇到流式节点时按普通节点处理：消费整个 async generator 但不转发产出的事件。
        """
        cur = self._resolve_entry()
        hops = 0
        while cur != END:
            if hops >= self.MAX_HOPS:
                raise RuntimeError(f"图执行超过最大跳转次数 {self.MAX_HOPS}，可能存在死循环（最后节点 {cur!r}）")
            hops += 1
            state = await self._execute_node(cur, state)
            cur = self._next_node(cur, state)
        return state

    async def run_stream(self, state: Any) -> AsyncGenerator[dict, None]:
        """流式执行图（Task 5 完整版）。

        语义：
        - **普通节点**（``add_node``）：以 ``async def fn(state) -> state`` 形式执行。
          节点可以把事件 ``{"type": ..., "data": {...}}`` 追加到 ``state.pending_events``，
          引擎在节点结束后**统一 flush** 这些事件并清空缓冲区。适合 thinking / servants /
          clarification 等"中间产物"事件。
        - **流式节点**（``add_stream_node``）：以 ``async def fn(state)`` 的 async generator
          形式执行。yield 的内容若是含 ``"type"`` 字段的 dict，引擎**实时转发**给上层；
          否则视为新的 state 实例（覆盖当前 state，作为下一节点输入）。适合需要逐 token
          推送 ``delta`` 事件的 LLM 流式生成节点。
        - **事件契约**：本方法只产出形如 ``{"type": <event_name>, "data": <payload>}`` 的内部
          事件 dict；不耦合 SSE 字符串格式。上层（如 ``main.chat_stream``）负责把它转换为
          ``sse_event(...)`` 字符串。

        Example::

            async for ev in graph.run_stream(state):
                yield sse_event(ev["type"], ev["data"])
        """
        cur = self._resolve_entry()
        hops = 0
        while cur != END:
            if hops >= self.MAX_HOPS:
                raise RuntimeError(f"图执行超过最大跳转次数 {self.MAX_HOPS}（最后节点 {cur!r}）")
            hops += 1
            if cur in self._stream_nodes:
                # 流式节点：实时 yield 事件；non-event yield 视为新 state
                agen = self._stream_nodes[cur](state)
                async for produced in agen:
                    if isinstance(produced, dict) and "type" in produced:
                        yield produced
                    elif produced is not None:
                        state = produced
            elif cur in self._nodes:
                new_state = await self._nodes[cur](state)
                if new_state is not None:
                    state = new_state
            else:
                raise GraphConfigError(f"节点 {cur!r} 未注册")
            # 节点结束后 flush 累积事件（普通节点的中间产物）
            pending = getattr(state, "pending_events", None)
            if pending:
                for ev in pending:
                    yield ev
                pending.clear()
            cur = self._next_node(cur, state)

    async def resume(
        self,
        state: Any,
        from_node: str,
        *,
        checkpointer: Checkpointer | None = None,
        session_id: str | None = None,
    ) -> Any:
        """从指定节点恢复执行。

        两种使用方式：

        1. 直接传入 state（用于测试或调用方已自行加载快照）::

               state = await graph.resume(state, "execute")

        2. 传入 checkpointer + session_id，由引擎从 checkpointer 加载 state::

               state = await graph.resume(None, "execute",
                                          checkpointer=cp, session_id=sid)

        Args:
            state: 要恢复的 PipelineState；若为 None，则从 checkpointer 加载。
            from_node: 恢复入口节点（必须已注册）。
            checkpointer: 可选，从中加载 state；与 session_id 配合使用。
            session_id: 可选，加载时使用的 key；不能为空字符串。

        Raises:
            GraphConfigError: from_node 未注册。
            ValueError: state 为 None 且未提供有效的 checkpointer + session_id。
            LookupError: checkpointer 中找不到对应 session_id 的快照。
        """
        self._validate_node_exists(from_node, where="resume.from_node")
        if state is None:
            if checkpointer is None or not session_id:
                raise ValueError("resume: state 为 None 时必须同时提供 checkpointer 与 session_id")
            state = checkpointer.load(session_id)
            if state is None:
                raise LookupError(f"resume: checkpointer 中未找到 session_id={session_id!r} 的快照（可能已过期）")
        cur = from_node
        hops = 0
        while cur != END:
            if hops >= self.MAX_HOPS:
                raise RuntimeError(f"resume 执行超过最大跳转次数 {self.MAX_HOPS}（最后节点 {cur!r}）")
            hops += 1
            state = await self._execute_node(cur, state)
            cur = self._next_node(cur, state)
        return state

    # ── 内部辅助 ──────────────────────────────────────────────

    async def _execute_node(self, name: str, state: Any) -> Any:
        if name in self._nodes:
            new_state = await self._nodes[name](state)
            return new_state if new_state is not None else state
        if name in self._stream_nodes:
            # 流式节点：消费 generator，最后一次 yield 视为 state；不转发事件
            agen = self._stream_nodes[name](state)
            last_state = state
            async for produced in agen:
                # 节点可能 yield SSE 事件 dict 或 state；我们仅用 state
                # （Task 5 会区分事件类型）
                if isinstance(produced, dict) and "type" in produced:
                    state.pending_events.append(produced)
                else:
                    last_state = produced
            return last_state
        raise GraphConfigError(f"节点 {name!r} 未注册")

    def _next_node(self, cur: str, state: Any) -> str:
        if cur in self._cond_edges:
            nxt = self._cond_edges[cur](state)
            if nxt != END and nxt not in self._nodes and nxt not in self._stream_nodes:
                raise GraphConfigError(f"节点 {cur!r} 的条件边路由到未注册的节点 {nxt!r}")
            return nxt
        if cur in self._static_edges:
            return self._static_edges[cur]
        # 没有任何出边 → 默认终止
        return END

    def _resolve_entry(self) -> str:
        if self._entry is None:
            raise GraphConfigError("图未设置 entry 节点（请先调用 set_entry）")
        return self._entry

    def _validate_node_exists(self, name: str, *, where: str) -> None:
        if name not in self._nodes and name not in self._stream_nodes:
            raise GraphConfigError(f"{where}: 节点 {name!r} 未注册")
