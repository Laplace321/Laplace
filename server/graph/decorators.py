"""节点装饰器：为节点统一附加重试 / 追踪等横切关注点。

`with_trace` 是节点埋点的标准入口（v0.5.1 全链路埋点改造）：
- 自动从 ContextVar 读取当前 trace_id（节点函数无需关心）
- 在节点执行前后写入 ``{event_name}_input`` / ``{event_name}_output`` 事件
- 节点抛出异常时自动写入 ``{event_name}_error`` 事件，再 re-raise 保留栈
- output 事件携带 latency_ms、result、metric_labels 切片，供 BI 多维聚合
- 同时上报 Prometheus 节点延迟（软依赖：metrics 模块未提供时静默跳过）
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from server.logger import get_trace_id, log_trace_event

NodeFn = Callable[[Any], Awaitable[Any]]


def with_retry(times: int = 2, delay: float = 0.0) -> Callable[[NodeFn], NodeFn]:
    """节点级重试装饰器。

    Example::

        @with_retry(times=2)
        async def classify_node(state):
            ...
    """

    def decorator(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        async def wrapper(state: Any) -> Any:
            last_err: Exception | None = None
            for attempt in range(times + 1):
                try:
                    return await fn(state)
                except Exception as err:  # noqa: BLE001
                    last_err = err
                    if attempt < times and delay > 0:
                        await asyncio.sleep(delay)
            # 所有重试均失败 → 抛出最后一次错误
            assert last_err is not None
            raise last_err

        return wrapper

    return decorator


def with_trace(event_name: str) -> Callable[[NodeFn], NodeFn]:
    """节点执行前后自动写入标准化 trace 事件。

    Args:
        event_name: 节点对应的 phase 前缀（建议引用 ``server.logger.Phase``）。
            将派生出 ``{event_name}_input`` / ``{event_name}_output`` /
            ``{event_name}_error`` 三类事件。

    事件 schema：
        - input  ``{node_name, user_message_preview, turn_type, session_id}``
        - output ``{node_name, latency_ms, result, metric_labels, reply_preview, count}``
        - error  ``{node_name, latency_ms, error_type}`` + level=ERROR + error 字段

    Example::

        from server.logger import Phase

        @with_trace(Phase.CLASSIFIER_OUTPUT)
        async def classify_node(state):
            ...
    """

    def decorator(fn: NodeFn) -> NodeFn:
        node_name = fn.__name__

        @wraps(fn)
        async def wrapper(state: Any) -> Any:
            # trace_id 优先取 state.trace_id（兼容显式传递的入口），其次 ContextVar
            trace_id = getattr(state, "trace_id", None) or get_trace_id()
            user_message = getattr(state, "user_message", "") or ""
            await log_trace_event(
                trace_id,
                f"{event_name}_input",
                {
                    "node_name": node_name,
                    "user_message_preview": user_message[:200],
                    "turn_type": getattr(state, "turn_type", ""),
                    "session_id": getattr(state, "session_id", ""),
                },
            )

            start = time.perf_counter()
            try:
                new_state = await fn(state)
            except Exception as err:  # noqa: BLE001
                latency_ms = (time.perf_counter() - start) * 1000.0
                await log_trace_event(
                    trace_id,
                    f"{event_name}_error",
                    {
                        "node_name": node_name,
                        "latency_ms": round(latency_ms, 2),
                        "error_type": type(err).__name__,
                    },
                    error=str(err)[:500],
                )
                _record_node_latency(node_name, "error", latency_ms)
                raise

            latency_ms = (time.perf_counter() - start) * 1000.0
            result_state = new_state if new_state is not None else state
            metric_labels = dict(getattr(result_state, "metric_labels", {}) or {})
            await log_trace_event(
                trace_id,
                f"{event_name}_output",
                {
                    "node_name": node_name,
                    "latency_ms": round(latency_ms, 2),
                    "result": "success",
                    "metric_labels": metric_labels,
                    "reply_preview": (getattr(result_state, "reply", "") or "")[:200],
                    "count": getattr(result_state, "count", 0),
                },
            )
            _record_node_latency(node_name, "success", latency_ms)
            return result_state

        return wrapper

    return decorator


def _record_node_latency(node_name: str, result: str, latency_ms: float) -> None:
    """软依赖上报节点延迟：metrics 未提供该 API 时静默跳过。

    Task 5 会在 ``server.monitor.metrics`` 中实现 ``record_node_latency``。
    """
    try:
        from server.monitor.metrics import get_collector

        collector = get_collector()
        method = getattr(collector, "record_node_latency", None)
        if method is None:
            return
        method(node_name, result, latency_ms)
    except Exception:  # noqa: BLE001
        # 监控失败绝不影响业务流程
        return
