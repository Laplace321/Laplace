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

    # ── A 链路（Skill 路由 + 执行）────────────────────────────
    skill_calls: list[dict] = field(default_factory=list)
    response_skill_name: str = "respond_servant_list"
    target_pipeline: str = "A"

    # ── 累计追踪 ──
    model_used: str = "skill_mode"
    trace_total_tokens: int = 0

    # ── 多轮对话（Task 4 Batch B）─────────────────────────────
    # session_id：前端 UUID，标识跨请求会话；为空时按单轮处理（不查 prev_turn / 不写 save_turn）
    session_id: str = ""
    # turn_type：本轮分类后的语义类型（MAJOR/MINOR/CORRECTION），由 classify_node 写入
    turn_type: str = "MAJOR"

    # ── 节点输出（最终拼装为 ChatResponse 的字段）──
    reply: str = ""
    servants: list[dict] = field(default_factory=list)
    count: int = 0
    query: dict[str, Any] = field(default_factory=dict)

    # ── SSE 流式专用（Task 5 启用，本期保留字段位）──
    pending_events: list[dict] = field(default_factory=list)

    # ── 运行时元数据（节点间临时通信，避免循环引用业务模块）──
    extras: dict[str, Any] = field(default_factory=dict)

    # ── BI 维度标签（v0.5.1 Task 3）──
    # 各节点在自己阶段产出后回填，generate_node 的 final 事件 + 监控指标会消费这些 label。
    # 包含：turn_type / pipeline / skill_names / clarification_type / error_reason
    #      / latency_bucket / model / has_prev_turn / total_tokens
    metric_labels: dict[str, str | int] = field(default_factory=dict)
