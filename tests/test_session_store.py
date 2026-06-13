"""Tests for server.graph.session (Task 4 Batch A)."""

from __future__ import annotations

import pickle

import pytest

from server.graph.checkpointer import InMemoryCheckpointer, SqliteCheckpointer
from server.graph.session import (
    PREV_SUMMARY_MAX_CHARS,
    SessionStore,
    TurnSnapshot,
)

# ────────────────────────────────────────────────────────────
# TurnSnapshot
# ────────────────────────────────────────────────────────────


def test_turn_snapshot_is_frozen():
    snap = TurnSnapshot(session_id="sid", summary="ok")
    with pytest.raises(Exception):  # FrozenInstanceError 是 dataclasses 内部异常
        snap.summary = "changed"  # type: ignore[misc]


def test_turn_snapshot_default_fields():
    snap = TurnSnapshot(session_id="sid")
    assert snap.user_message == ""
    assert snap.reply == ""
    assert snap.summary == ""
    assert snap.pipeline == "A"
    assert snap.skill_calls == []
    assert snap.response_skill_name == "respond_servant_list"
    assert snap.servants == []
    assert snap.query == {}
    assert snap.turn_type == "MAJOR"
    assert snap.timestamp == 0.0


def test_turn_snapshot_truncated_summary_short_passthrough():
    snap = TurnSnapshot(session_id="sid", summary="短摘要")
    assert snap.truncated_summary() == "短摘要"


def test_turn_snapshot_truncated_summary_truncates_long():
    long_summary = "x" * 500
    snap = TurnSnapshot(session_id="sid", summary=long_summary)
    out = snap.truncated_summary()
    assert len(out) == PREV_SUMMARY_MAX_CHARS
    assert out.endswith("…")


def test_turn_snapshot_truncated_summary_custom_limit():
    snap = TurnSnapshot(session_id="sid", summary="abcdefg")
    assert snap.truncated_summary(max_chars=4) == "abc…"


def test_turn_snapshot_truncated_summary_empty():
    snap = TurnSnapshot(session_id="sid", summary="")
    assert snap.truncated_summary() == ""


def test_turn_snapshot_pickle_roundtrip():
    """TurnSnapshot 必须可 pickle，否则无法落 SqliteCheckpointer。"""
    snap = TurnSnapshot(
        session_id="sid",
        user_message="hello",
        reply="hi",
        summary="greet",
        pipeline="A",
        skill_calls=[{"skill_name": "lookup_servant", "params": {}}],
        servants=[{"collectionNo": 100, "name": "Saber"}],
        query={"servants": []},
        turn_type="MINOR",
        timestamp=123.0,
    )
    restored = pickle.loads(pickle.dumps(snap))
    assert restored == snap


# ────────────────────────────────────────────────────────────
# SessionStore — Turn 命名空间
# ────────────────────────────────────────────────────────────


def _make_store():
    return SessionStore(InMemoryCheckpointer(ttl_seconds=60))


def test_session_save_and_load_turn():
    store = _make_store()
    snap = TurnSnapshot(session_id="sid", summary="last turn")
    store.save_turn(snap)
    loaded = store.load_prev_turn("sid")
    assert loaded == snap


def test_session_load_prev_turn_missing_returns_none():
    store = _make_store()
    assert store.load_prev_turn("nonexistent") is None


def test_session_load_prev_turn_empty_id_returns_none():
    store = _make_store()
    assert store.load_prev_turn("") is None


def test_session_save_turn_empty_session_id_raises():
    store = _make_store()
    with pytest.raises(ValueError):
        store.save_turn(TurnSnapshot(session_id=""))


def test_session_save_turn_overwrites():
    store = _make_store()
    store.save_turn(TurnSnapshot(session_id="sid", summary="v1"))
    store.save_turn(TurnSnapshot(session_id="sid", summary="v2"))
    assert store.load_prev_turn("sid").summary == "v2"


def test_session_load_prev_turn_invalid_type_drops_record():
    """checkpointer 中 turn 命名空间数据被污染（非 TurnSnapshot）时应自动删除。"""
    cp = InMemoryCheckpointer(ttl_seconds=60)
    store = SessionStore(cp)
    # 直接绕过 store 的类型校验，模拟数据污染
    cp.save("turn:sid", {"not": "a snapshot"})
    assert store.load_prev_turn("sid") is None
    # 应已被删除
    assert cp.load("turn:sid") is None


def test_session_clear_session_removes_turn_and_pending():
    store = _make_store()
    store.save_turn(TurnSnapshot(session_id="sid", summary="x"))
    store.save_pending("sid", {"state": "pending"})
    store.clear_session("sid")
    assert store.load_prev_turn("sid") is None
    assert store.load_pending("sid") is None


def test_session_clear_session_empty_is_noop():
    store = _make_store()
    store.clear_session("")  # should not raise


# ────────────────────────────────────────────────────────────
# SessionStore — Pending 命名空间
# ────────────────────────────────────────────────────────────


def test_session_pending_save_and_load():
    store = _make_store()
    state = {"trace_id": "abc", "extras": {"bail_out": "interrupt"}}
    store.save_pending("sid", state)
    loaded = store.load_pending("sid")
    assert loaded == state


def test_session_pending_missing_returns_none():
    store = _make_store()
    assert store.load_pending("nonexistent") is None
    assert store.has_pending("nonexistent") is False


def test_session_has_pending_true_after_save():
    store = _make_store()
    store.save_pending("sid", "anything")
    assert store.has_pending("sid") is True


def test_session_clear_pending_removes_only_pending():
    """clear_pending 不应影响 turn 命名空间。"""
    store = _make_store()
    store.save_turn(TurnSnapshot(session_id="sid", summary="keep me"))
    store.save_pending("sid", "drop me")
    store.clear_pending("sid")
    assert store.has_pending("sid") is False
    assert store.load_prev_turn("sid") is not None


def test_session_pending_save_empty_id_raises():
    store = _make_store()
    with pytest.raises(ValueError):
        store.save_pending("", "x")


def test_session_turn_and_pending_are_isolated():
    """同一 session_id，turn 和 pending 互不干扰。"""
    store = _make_store()
    store.save_turn(TurnSnapshot(session_id="sid", summary="turn"))
    store.save_pending("sid", "pending")
    assert store.load_prev_turn("sid").summary == "turn"
    assert store.load_pending("sid") == "pending"


# ────────────────────────────────────────────────────────────
# SessionStore — 维护
# ────────────────────────────────────────────────────────────


def test_session_cleanup_expired_delegates_to_checkpointer():
    cp = InMemoryCheckpointer(ttl_seconds=60)
    store = SessionStore(cp)
    store.save_turn(TurnSnapshot(session_id="sid", summary="x"))
    # 手动让其过期
    import time as _t

    cp._store["turn:sid"] = (_t.time() - 100, cp._store["turn:sid"][1])
    deleted = store.cleanup_expired()
    assert deleted == 1


def test_session_checkpointer_property():
    cp = InMemoryCheckpointer()
    store = SessionStore(cp)
    assert store.checkpointer is cp


# ────────────────────────────────────────────────────────────
# 端到端：SqliteCheckpointer + SessionStore 持久化 TurnSnapshot
# ────────────────────────────────────────────────────────────


def test_sqlite_session_store_persists_turn_across_instances(tmp_path):
    db_path = tmp_path / "ck.db"
    cp1 = SqliteCheckpointer(db_path)
    try:
        store1 = SessionStore(cp1)
        snap = TurnSnapshot(
            session_id="sid",
            user_message="清姬有什么宝具",
            reply="...",
            summary="询问清姬宝具",
            pipeline="A",
            turn_type="MAJOR",
        )
        store1.save_turn(snap)
    finally:
        cp1.close()

    cp2 = SqliteCheckpointer(db_path)
    try:
        store2 = SessionStore(cp2)
        loaded = store2.load_prev_turn("sid")
        assert loaded is not None
        assert loaded.summary == "询问清姬宝具"
        assert loaded.turn_type == "MAJOR"
    finally:
        cp2.close()
