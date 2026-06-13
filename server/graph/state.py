"""PipelineState — 流经图引擎的类型化状态对象。

ADR-028 的核心抽象：所有节点输入输出都是这个 dataclass，节点之间通过共享 state 通信。
本期 (v0.5.0 Task 1) 仅承载 B/C 链路所需字段；Task 2-4 会逐步扩展 routing/skill_calls/turn_type 等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineState:
    """图引擎的流转状态。

    所有节点函数签名均为 ``async def node(state: PipelineState) -> PipelineState``，
    节点应原地修改 state 字段，并返回同一 state 实例（dataclass mutable）。
    """

    # ── 输入 ──
    user_message: str = ""
    trace_id: str = ""
    request_start: float = 0.0
    client_ip: str = "unknown"

    # ── 路由分类 ──
    classified_pipeline: str = "A"  # A / B / C
    classifier_confidence: float = 1.0
    classifier_model: str = "unknown"

    # ── B 链路（Atlas）专用 ──
    atlas_query: dict[str, Any] | None = None

    # ── 累计追踪 ──
    model_used: str = "skill_mode"
    trace_total_tokens: int = 0

    # ── 节点输出（最终拼装为 ChatResponse 的字段）──
    reply: str = ""
    servants: list[dict] = field(default_factory=list)
    count: int = 0
    query: dict[str, Any] = field(default_factory=dict)

    # ── SSE 流式专用（Task 5 启用，本期保留字段位）──
    pending_events: list[dict] = field(default_factory=list)

    # ── 运行时元数据（节点间临时通信，避免循环引用业务模块）──
    extras: dict[str, Any] = field(default_factory=dict)
