"""自研轻量化 DAG 图引擎（ADR-028）。

提供 4 个核心抽象：
- PipelineState: 类型化 dataclass，流经全图
- 节点 = async 纯函数 State -> State
- 条件边 = 纯函数 State -> 下一节点名
- StateGraph: ~200 行图引擎，支持 run / run_stream / resume

Task 4 Batch A：新增 Checkpointer / SessionStore，支持多轮对话状态持久化与
系统主动中断恢复（业务节点接入由 Batch B 完成）。
"""

from server.graph.checkpointer import (
    DEFAULT_TTL_SECONDS,
    Checkpointer,
    InMemoryCheckpointer,
    SqliteCheckpointer,
)
from server.graph.engine import END, StateGraph
from server.graph.session import PREV_SUMMARY_MAX_CHARS, SessionStore, TurnSnapshot
from server.graph.state import PipelineState

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "END",
    "PREV_SUMMARY_MAX_CHARS",
    "Checkpointer",
    "InMemoryCheckpointer",
    "PipelineState",
    "SessionStore",
    "SqliteCheckpointer",
    "StateGraph",
    "TurnSnapshot",
]
