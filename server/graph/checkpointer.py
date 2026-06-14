"""Checkpointer — 多轮对话状态持久化抽象（ADR-028 / Task 4 Batch A）。

设计目标：
- ``Checkpointer`` Protocol：定义统一接口（save / load / delete / cleanup_expired）
- ``InMemoryCheckpointer``：进程内字典实现，单测和单机灰度使用
- ``SqliteCheckpointer``：WAL 模式 SQLite 实现，落 ``server/data/checkpoints.db``，30 分钟 TTL

存储内容：
- key = ``session_id`` (str)
- value = pickle 序列化后的 ``PipelineState`` 快照（含 turn_type、skill_calls、servants 等）

并发：
- SQLite WAL 模式允许多读单写，每个连接绑定线程（``check_same_thread=True``）。
- 应用层使用 ``threading.RLock`` 保护实例方法，避免 cursor 跨线程复用。

TTL：
- 每条记录写入时记 ``updated_at``（unix epoch 秒）。
- ``cleanup_expired(now)`` 删除 ``now - updated_at > ttl`` 的记录；调用时机由上层（SessionStore / 后台任务）决定。
- 不在 save / load 中自动触发，避免高频 IO。
"""

from __future__ import annotations

import logging
import os
import pickle
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: 默认 TTL：30 分钟（秒）
DEFAULT_TTL_SECONDS = 30 * 60


@runtime_checkable
class Checkpointer(Protocol):
    """状态持久化抽象。所有实现都必须线程安全。"""

    def save(self, session_id: str, state: Any) -> None:
        """保存（或覆盖）会话状态。

        Args:
            session_id: 会话唯一标识（前端生成的 UUID）。
            state: 任意可 pickle 对象，通常为 PipelineState。
        """
        ...

    def load(self, session_id: str) -> Any | None:
        """加载会话状态；不存在或已过期返回 ``None``。"""
        ...

    def delete(self, session_id: str) -> None:
        """删除会话状态；不存在则静默忽略。"""
        ...

    def cleanup_expired(self, now: float | None = None) -> int:
        """清理过期记录，返回删除条数。``now`` 为 None 时使用当前 unix 时间戳。"""
        ...


# ────────────────────────────────────────────────────────────
# InMemoryCheckpointer
# ────────────────────────────────────────────────────────────


class InMemoryCheckpointer:
    """进程内字典实现，零依赖。单测 / 单机调试使用。

    数据结构：``{session_id: (updated_at, state_obj)}``，state_obj 为浅拷贝（pickle round-trip
    在 Sqlite 实现中执行；InMemory 不深拷贝以节省开销，调用方负责不在 save 后修改原对象）。
    """

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 必须 > 0，当前为 {ttl_seconds}")
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def save(self, session_id: str, state: Any) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        with self._lock:
            self._store[session_id] = (time.time(), state)

    def load(self, session_id: str) -> Any | None:
        if not session_id:
            return None
        with self._lock:
            entry = self._store.get(session_id)
            if entry is None:
                return None
            updated_at, state = entry
            if time.time() - updated_at > self._ttl:
                # 过期，惰性清理
                self._store.pop(session_id, None)
                return None
            return state

    def delete(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._store.pop(session_id, None)

    def cleanup_expired(self, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - self._ttl
        with self._lock:
            expired = [sid for sid, (ts, _) in self._store.items() if ts <= cutoff]
            for sid in expired:
                self._store.pop(sid, None)
            return len(expired)


# ────────────────────────────────────────────────────────────
# SqliteCheckpointer
# ────────────────────────────────────────────────────────────


class SqliteCheckpointer:
    """SQLite 实现，WAL 模式，磁盘持久化。

    表结构::

        CREATE TABLE IF NOT EXISTS checkpoints (
            session_id TEXT PRIMARY KEY,
            payload BLOB NOT NULL,
            updated_at REAL NOT NULL
        );

    Payload 使用 ``pickle.dumps`` 序列化任意 Python 对象，包括 dataclass 实例。

    线程安全：每个实例持有一个 sqlite3 connection（``check_same_thread=False``，允许跨线程使用），
    并通过 ``threading.RLock`` 保护游标操作。WAL 模式允许多读单写。
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        ensure_dir: bool = True,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds 必须 > 0，当前为 {ttl_seconds}")
        self._ttl = ttl_seconds
        self._db_path = Path(db_path)
        if ensure_dir:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 异步路由可能在不同线程访问；锁保护并发
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit；显式 BEGIN/COMMIT
        )
        self._lock = threading.RLock()
        self._init_schema()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    session_id TEXT PRIMARY KEY,
                    payload BLOB NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_updated_at ON checkpoints(updated_at)")

    def save(self, session_id: str, state: Any) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        try:
            payload = pickle.dumps(state)
        except (pickle.PicklingError, TypeError, AttributeError) as exc:
            raise ValueError(f"state 无法序列化：{exc}") from exc
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO checkpoints(session_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (session_id, payload, time.time()),
            )

    def load(self, session_id: str) -> Any | None:
        if not session_id:
            return None
        with self._lock:
            cur = self._conn.execute(
                "SELECT payload, updated_at FROM checkpoints WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            payload, updated_at = row
            if time.time() - updated_at > self._ttl:
                # 过期，惰性清理
                self._conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
                return None
        try:
            return pickle.loads(payload)
        except (pickle.UnpicklingError, EOFError, AttributeError) as exc:
            logger.warning(
                "checkpoint payload 反序列化失败，删除该记录 session_id=%s err=%s",
                session_id,
                exc,
            )
            self.delete(session_id)
            return None

    def delete(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))

    def cleanup_expired(self, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - self._ttl
        with self._lock:
            cur = self._conn.execute("DELETE FROM checkpoints WHERE updated_at <= ?", (cutoff,))
            return cur.rowcount or 0

    def close(self) -> None:
        """关闭底层连接（测试用；生产实例应进程级长存）。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "Checkpointer",
    "InMemoryCheckpointer",
    "SqliteCheckpointer",
]
