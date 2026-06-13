"""SessionStore — 多轮对话会话管理（ADR-028 / Task 4 Batch A）。

职责：
- 在 ``Checkpointer`` 之上封装多轮对话语义层。
- ``TurnSnapshot``：单轮对话的不可变快照，承载 LLM 分类时所需的「上一轮摘要」与可能用于 MINOR 合并的关键状态。
- ``SessionStore``：维护「session_id → 上一轮 TurnSnapshot」+「session_id → 待恢复 PipelineState（pending 中断态）」两个独立命名空间，复用同一 Checkpointer 后端但 key 加前缀隔离。

关键约定：
- 每个 session 只保留 *最近一轮* TurnSnapshot（v0.5.x 不做长会话历史；后续版本如需可改为列表）。
- ``save_turn`` 在节点末尾调用（generate 节点产出最终 reply 后），写入快照。
- ``load_prev_turn`` 在 classify 节点入口调用，用于注入 prev_summary。
- ``clear_session`` 在 MAJOR 切换时调用，避免 MINOR/CORRECTION 标记位残留（ADR-026 教训）。
- pending（系统中断）路径独立 key，避免与 turn 历史互相污染。

序列化：
- ``TurnSnapshot`` 是 frozen dataclass，pickle 友好；调用方传入的可变对象（list/dict）会被原样持久化，
  调用方应负责保证「只读快照」语义（如必要可在调用前 ``copy.deepcopy``）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.graph.checkpointer import Checkpointer

logger = logging.getLogger(__name__)

# Checkpointer key 前缀，避免 turn 与 pending 互相污染
_TURN_PREFIX = "turn:"
_PENDING_PREFIX = "pending:"

#: 上一轮摘要传入分类器的最大长度（防 prompt 爆炸）
PREV_SUMMARY_MAX_CHARS = 200


@dataclass(frozen=True)
class TurnSnapshot:
    """单轮对话的只读快照，用于多轮对话时给下一轮分类器提供上下文。

    Attributes:
        session_id: 会话 ID（前端 UUID）。
        user_message: 本轮用户原始输入。
        reply: 本轮系统最终回复。
        summary: 系统总结的「上一轮在做什么」，给下一轮 classifier prompt 使用。
                 ≤200 字符；超长会被截断。
        pipeline: 本轮命中的管线（A / B / C）。
        skill_calls: 本轮执行的 skill_calls（list[dict]），用于 MINOR G2 追加过滤。
        response_skill_name: 本轮使用的回复技能名（如 respond_servant_list），用于 MINOR G3 切换。
        servants: 本轮返回的从者列表（精简版，只保留 collectionNo / name），
                  用于 MINOR CORRECTION 时锚定上下文。
        query: 本轮组装好的 query 字段（透传给前端的 ChatResponse.query）。
        turn_type: 本轮的 turn_type（MAJOR/MINOR/CORRECTION），用于审计与下游策略。
        timestamp: 写入时间（unix 秒），由 Checkpointer 内部维护，此处仅作冗余。
    """

    session_id: str
    user_message: str = ""
    reply: str = ""
    summary: str = ""
    pipeline: str = "A"
    skill_calls: list[dict] = field(default_factory=list)
    response_skill_name: str = "respond_servant_list"
    servants: list[dict] = field(default_factory=list)
    query: dict[str, Any] = field(default_factory=dict)
    turn_type: str = "MAJOR"
    timestamp: float = 0.0

    def truncated_summary(self, max_chars: int = PREV_SUMMARY_MAX_CHARS) -> str:
        """返回截断后的 summary，用于注入分类器 prompt。"""
        if not self.summary:
            return ""
        if len(self.summary) <= max_chars:
            return self.summary
        return self.summary[: max_chars - 1] + "…"


class SessionStore:
    """会话管理：在 Checkpointer 之上封装 turn / pending 两条命名空间。

    Example::

        store = SessionStore(SqliteCheckpointer("server/data/checkpoints.db"))
        store.save_turn(snapshot)            # generate 节点末尾
        prev = store.load_prev_turn(sid)     # classify 节点入口
        store.clear_session(sid)             # MAJOR 切换时
        store.save_pending(sid, state)       # 系统主动中断节点
        state = store.load_pending(sid)      # /chat/resume 入口
        store.clear_pending(sid)             # resume 完成后
    """

    def __init__(self, checkpointer: Checkpointer) -> None:
        self._cp = checkpointer

    @property
    def checkpointer(self) -> Checkpointer:
        return self._cp

    # ── Turn 历史 ────────────────────────────────────────────

    def save_turn(self, snapshot: TurnSnapshot) -> None:
        """保存单轮快照（覆盖该 session 上一轮）。"""
        if not snapshot.session_id:
            raise ValueError("TurnSnapshot.session_id 不能为空")
        self._cp.save(_TURN_PREFIX + snapshot.session_id, snapshot)

    def load_prev_turn(self, session_id: str) -> TurnSnapshot | None:
        """加载该 session 最近一轮快照；不存在或已过期返回 None。"""
        if not session_id:
            return None
        snapshot = self._cp.load(_TURN_PREFIX + session_id)
        if snapshot is None:
            return None
        if not isinstance(snapshot, TurnSnapshot):
            logger.warning(
                "checkpointer 中 turn 数据类型异常 session_id=%s type=%s，已丢弃",
                session_id,
                type(snapshot).__name__,
            )
            self._cp.delete(_TURN_PREFIX + session_id)
            return None
        return snapshot

    def clear_session(self, session_id: str) -> None:
        """清除该 session 的 turn 历史 + pending 中断态。

        MAJOR 切换或显式新会话时调用，避免上一轮的 skill_calls / pending 状态影响新轮次。
        """
        if not session_id:
            return
        self._cp.delete(_TURN_PREFIX + session_id)
        self._cp.delete(_PENDING_PREFIX + session_id)

    # ── Pending（系统主动中断）─────────────────────────────────

    def save_pending(self, session_id: str, state: Any) -> None:
        """保存「等待用户补充信息」的 PipelineState（系统主动中断时调用）。"""
        if not session_id:
            raise ValueError("session_id 不能为空")
        self._cp.save(_PENDING_PREFIX + session_id, state)

    def load_pending(self, session_id: str) -> Any | None:
        """加载该 session 的 pending PipelineState；不存在或已过期返回 None。"""
        if not session_id:
            return None
        return self._cp.load(_PENDING_PREFIX + session_id)

    def clear_pending(self, session_id: str) -> None:
        """resume 成功后调用，删除 pending 记录。"""
        if not session_id:
            return
        self._cp.delete(_PENDING_PREFIX + session_id)

    def has_pending(self, session_id: str) -> bool:
        """检查是否存在待恢复的 pending 状态。"""
        return self.load_pending(session_id) is not None

    # ── 维护 ─────────────────────────────────────────────────

    def cleanup_expired(self, now: float | None = None) -> int:
        """委托底层 Checkpointer 清理过期记录，返回删除条数。"""
        return self._cp.cleanup_expired(now)


__all__ = [
    "PREV_SUMMARY_MAX_CHARS",
    "SessionStore",
    "TurnSnapshot",
]
