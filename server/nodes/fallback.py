"""Pipeline A 降级节点 — 模板回复（greeting / out_of_scope — ADR-028 Task 3）。

迁移自 ``server/pipeline.py`` 的 ``_bail_out_to_chat_response`` 中的两个模板分支：

- ``fallback_greeting``     路由判定为打招呼，返回 GREETING 模板
- ``fallback_out_of_scope`` 路由判定为超出范围，返回 OUT_OF_SCOPE 模板

节点行为：从 FALLBACK_TEMPLATES 取出对应模板（按 fb.code.upper() 索引），
写 state.reply / query=routing_result，作为终态节点连 END。
"""

from __future__ import annotations

import time

from server.fallback import FALLBACK_TEMPLATES
from server.graph.state import PipelineState
from server.logger import log_trace_event


async def template_fallback_node(state: PipelineState) -> PipelineState:
    """模板回复节点：根据 routing_result.fallback.code 写入预置文案。"""
    trace_id = state.trace_id
    request_start = state.request_start

    routing_result = state.extras.get("routing_result", {}) or {}
    fallback = routing_result.get("fallback", {}) or {}
    fb_code = fallback.get("code", "no_match")
    fb_msg = fallback.get("message", "无法理解你的问题，请尝试更具体的描述。")
    template_reply = FALLBACK_TEMPLATES.get(fb_code.upper(), fb_msg)

    await log_trace_event(
        trace_id,
        "final",
        {
            "total_time_ms": (time.monotonic() - request_start) * 1000,
            "result": f"fallback_{fb_code}",
            "mode": f"fallback_{fb_code}",
            "total_tokens": state.trace_total_tokens,
        },
    )

    state.reply = template_reply
    state.servants = []
    state.count = 0
    state.query = routing_result
    return state
