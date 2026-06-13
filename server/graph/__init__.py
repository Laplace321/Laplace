"""自研轻量化 DAG 图引擎（ADR-028）。

提供 4 个核心抽象：
- PipelineState: 类型化 dataclass，流经全图
- 节点 = async 纯函数 State -> State
- 条件边 = 纯函数 State -> 下一节点名
- StateGraph: ~200 行图引擎，支持 run / run_stream / resume

Task 1 仅落地 run() 同步执行 + 静态/条件边；run_stream() 在 Task 5 完善。
"""

from server.graph.engine import END, StateGraph
from server.graph.state import PipelineState

__all__ = ["END", "PipelineState", "StateGraph"]
