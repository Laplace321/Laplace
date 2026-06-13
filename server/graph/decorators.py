"""节点装饰器：为节点统一附加重试 / 追踪等横切关注点。

本期 Task 1 提供基础实现，Task 2/3 节点逐步使用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from server.logger import log_trace_event

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
    """节点执行前后自动记录 trace event。

    输入：写入 ``{event_name}_input`` 事件，包含 user_message。
    输出：写入 ``{event_name}_output`` 事件，包含 reply/count（如有）。

    节点自身的细粒度 log_trace_event 调用不受影响。
    """

    def decorator(fn: NodeFn) -> NodeFn:
        @wraps(fn)
        async def wrapper(state: Any) -> Any:
            trace_id = getattr(state, "trace_id", "unknown")
            await log_trace_event(
                trace_id,
                f"{event_name}_input",
                {"user_message": getattr(state, "user_message", "")[:200]},
            )
            new_state = await fn(state)
            result_state = new_state if new_state is not None else state
            await log_trace_event(
                trace_id,
                f"{event_name}_output",
                {
                    "reply_preview": getattr(result_state, "reply", "")[:200],
                    "count": getattr(result_state, "count", 0),
                },
            )
            return result_state

        return wrapper

    return decorator
