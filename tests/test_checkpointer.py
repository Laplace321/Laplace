"""Tests for server.graph.checkpointer (Task 4 Batch A)."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from server.graph.checkpointer import (
    DEFAULT_TTL_SECONDS,
    Checkpointer,
    InMemoryCheckpointer,
    SqliteCheckpointer,
)


@dataclass
class _SamplePayload:
    """用于测试 pickle 序列化的样本对象。"""

    name: str
    count: int = 0
    items: list[str] | None = None


# ────────────────────────────────────────────────────────────
# Protocol 一致性
# ────────────────────────────────────────────────────────────


def test_default_ttl_is_30_minutes():
    assert DEFAULT_TTL_SECONDS == 30 * 60


def test_inmemory_implements_protocol():
    cp = InMemoryCheckpointer()
    assert isinstance(cp, Checkpointer)


def test_sqlite_implements_protocol(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    assert isinstance(cp, Checkpointer)
    cp.close()


# ────────────────────────────────────────────────────────────
# InMemoryCheckpointer
# ────────────────────────────────────────────────────────────


def test_inmemory_save_and_load_roundtrip():
    cp = InMemoryCheckpointer()
    payload = _SamplePayload(name="foo", count=3, items=["a", "b"])
    cp.save("sid-1", payload)
    loaded = cp.load("sid-1")
    assert loaded is payload  # InMemory 不深拷贝
    assert loaded.name == "foo"


def test_inmemory_load_missing_returns_none():
    cp = InMemoryCheckpointer()
    assert cp.load("nonexistent") is None


def test_inmemory_delete_removes_record():
    cp = InMemoryCheckpointer()
    cp.save("sid-1", _SamplePayload(name="x"))
    cp.delete("sid-1")
    assert cp.load("sid-1") is None


def test_inmemory_delete_missing_is_noop():
    cp = InMemoryCheckpointer()
    cp.delete("nonexistent")  # should not raise


def test_inmemory_save_overwrites():
    cp = InMemoryCheckpointer()
    cp.save("sid-1", _SamplePayload(name="v1"))
    cp.save("sid-1", _SamplePayload(name="v2"))
    assert cp.load("sid-1").name == "v2"


def test_inmemory_ttl_expiry_lazy_cleanup():
    cp = InMemoryCheckpointer(ttl_seconds=1)
    cp.save("sid-1", _SamplePayload(name="x"))
    # 手动篡改时间戳模拟过期，避免真实 sleep
    cp._store["sid-1"] = (time.time() - 5.0, cp._store["sid-1"][1])
    assert cp.load("sid-1") is None
    assert "sid-1" not in cp._store  # 惰性清理已删除


def test_inmemory_cleanup_expired_returns_count():
    cp = InMemoryCheckpointer(ttl_seconds=1)
    now = time.time()
    cp._store["expired1"] = (now - 100, "data1")
    cp._store["expired2"] = (now - 100, "data2")
    cp._store["fresh"] = (now, "data3")
    deleted = cp.cleanup_expired(now=now)
    assert deleted == 2
    assert "fresh" in cp._store
    assert "expired1" not in cp._store
    assert "expired2" not in cp._store


def test_inmemory_empty_session_id_raises_on_save():
    cp = InMemoryCheckpointer()
    with pytest.raises(ValueError):
        cp.save("", _SamplePayload(name="x"))


def test_inmemory_empty_session_id_returns_none_on_load():
    cp = InMemoryCheckpointer()
    assert cp.load("") is None


def test_inmemory_invalid_ttl_raises():
    with pytest.raises(ValueError):
        InMemoryCheckpointer(ttl_seconds=0)
    with pytest.raises(ValueError):
        InMemoryCheckpointer(ttl_seconds=-10)


# ────────────────────────────────────────────────────────────
# SqliteCheckpointer
# ────────────────────────────────────────────────────────────


def test_sqlite_save_and_load_roundtrip(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        payload = _SamplePayload(name="foo", count=3, items=["a", "b"])
        cp.save("sid-1", payload)
        loaded = cp.load("sid-1")
        assert loaded is not payload  # SQLite 经过 pickle round-trip
        assert isinstance(loaded, _SamplePayload)
        assert loaded.name == "foo"
        assert loaded.count == 3
        assert loaded.items == ["a", "b"]
    finally:
        cp.close()


def test_sqlite_load_missing_returns_none(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        assert cp.load("nonexistent") is None
    finally:
        cp.close()


def test_sqlite_delete_removes_record(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        cp.save("sid-1", _SamplePayload(name="x"))
        cp.delete("sid-1")
        assert cp.load("sid-1") is None
    finally:
        cp.close()


def test_sqlite_save_overwrites(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        cp.save("sid-1", _SamplePayload(name="v1"))
        cp.save("sid-1", _SamplePayload(name="v2"))
        loaded = cp.load("sid-1")
        assert loaded.name == "v2"
    finally:
        cp.close()


def test_sqlite_persists_across_instances(tmp_path):
    db_path = tmp_path / "ck.db"
    cp1 = SqliteCheckpointer(db_path)
    try:
        cp1.save("sid-1", _SamplePayload(name="persist"))
    finally:
        cp1.close()

    cp2 = SqliteCheckpointer(db_path)
    try:
        loaded = cp2.load("sid-1")
        assert loaded is not None
        assert loaded.name == "persist"
    finally:
        cp2.close()


def test_sqlite_ttl_expiry_lazy_cleanup(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db", ttl_seconds=1)
    try:
        cp.save("sid-1", _SamplePayload(name="x"))
        # 直接改 updated_at 列模拟过期
        with cp._lock:
            cp._conn.execute(
                "UPDATE checkpoints SET updated_at = ? WHERE session_id = ?",
                (time.time() - 100, "sid-1"),
            )
        assert cp.load("sid-1") is None
        # 惰性清理后应已物理删除
        with cp._lock:
            row = cp._conn.execute("SELECT 1 FROM checkpoints WHERE session_id = ?", ("sid-1",)).fetchone()
        assert row is None
    finally:
        cp.close()


def test_sqlite_cleanup_expired_returns_count(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db", ttl_seconds=1)
    try:
        cp.save("fresh", _SamplePayload(name="fresh"))
        cp.save("old1", _SamplePayload(name="old1"))
        cp.save("old2", _SamplePayload(name="old2"))
        # 手动让 old1/old2 过期
        with cp._lock:
            cp._conn.execute(
                "UPDATE checkpoints SET updated_at = ? WHERE session_id IN ('old1', 'old2')",
                (time.time() - 100,),
            )
        deleted = cp.cleanup_expired()
        assert deleted == 2
        assert cp.load("fresh") is not None
        assert cp.load("old1") is None
    finally:
        cp.close()


def test_sqlite_unpicklable_payload_raises(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        # lambda 不可 pickle
        with pytest.raises(ValueError, match="无法序列化"):
            cp.save("sid-1", lambda x: x)
    finally:
        cp.close()


def test_sqlite_concurrent_save_no_corruption(tmp_path):
    """多线程并发写入，验证 WAL 模式下数据完整性。"""
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:

        def worker(idx: int) -> None:
            cp.save(f"sid-{idx}", _SamplePayload(name=f"name-{idx}", count=idx))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(50)))

        # 所有 50 条都应可读
        for i in range(50):
            loaded = cp.load(f"sid-{i}")
            assert loaded is not None
            assert loaded.name == f"name-{i}"
            assert loaded.count == i
    finally:
        cp.close()


def test_sqlite_concurrent_save_load_same_key(tmp_path):
    """同一 key 多线程读写，应不抛异常（最终值为某次写入）。"""
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        cp.save("sid-1", _SamplePayload(name="initial"))
        errors: list[Exception] = []
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                try:
                    cp.save("sid-1", _SamplePayload(name=f"w{i}", count=i))
                    i += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

        def reader():
            while not stop.is_set():
                try:
                    cp.load("sid-1")
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=2)
        assert errors == []
    finally:
        cp.close()


def test_sqlite_corrupted_payload_drops_record(tmp_path):
    """payload 反序列化失败时应自动删除记录而非抛异常。"""
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        # 直接写入非法 payload
        with cp._lock:
            cp._conn.execute(
                "INSERT INTO checkpoints(session_id, payload, updated_at) VALUES (?, ?, ?)",
                ("sid-1", b"not-a-valid-pickle", time.time()),
            )
        loaded = cp.load("sid-1")
        assert loaded is None
        # 后续应已删除
        with cp._lock:
            row = cp._conn.execute("SELECT 1 FROM checkpoints WHERE session_id = ?", ("sid-1",)).fetchone()
        assert row is None
    finally:
        cp.close()


def test_sqlite_creates_parent_dir(tmp_path):
    nested = tmp_path / "nested" / "dir" / "ck.db"
    cp = SqliteCheckpointer(nested)
    try:
        assert nested.parent.exists()
        cp.save("sid-1", _SamplePayload(name="x"))
        assert cp.load("sid-1") is not None
    finally:
        cp.close()


def test_sqlite_empty_session_id_raises_on_save(tmp_path):
    cp = SqliteCheckpointer(tmp_path / "ck.db")
    try:
        with pytest.raises(ValueError):
            cp.save("", _SamplePayload(name="x"))
    finally:
        cp.close()


def test_sqlite_invalid_ttl_raises(tmp_path):
    with pytest.raises(ValueError):
        SqliteCheckpointer(tmp_path / "ck.db", ttl_seconds=0)
