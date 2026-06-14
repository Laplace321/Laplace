"""多轮对话集成测试（Task 4 Batch B — ADR-028）。

聚焦三层契约：
1. ``classify_node`` 在多轮场景下注入 prev_summary、解析 turn_type、MAJOR 时清空 session
2. ``edges`` 在 MINOR/CORRECTION + prev_turn 时跳过 route 走 merge_filters
3. ``generate_node`` 在配置 SessionStore 时把本轮快照写回 SessionStore

不直接依赖图引擎完整运行（避免 LLM/数据初始化），通过 monkeypatch chat_completion + mock
SessionStore 行为做最小集成验证。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from server.edges import after_classify, after_merge_filters
from server.graph.checkpointer import InMemoryCheckpointer
from server.graph.session import SessionStore, TurnSnapshot
from server.graph.state import PipelineState
from server.nodes.classify import classify_node

# ────────────────────────────────────────────────────────────
# classify_node — prev_turn 注入与 turn_type 解析
# ────────────────────────────────────────────────────────────


def _make_session_store() -> SessionStore:
    return SessionStore(InMemoryCheckpointer(ttl_seconds=60))


@pytest.mark.asyncio
async def test_classify_node_loads_prev_turn_and_writes_extras():
    store = _make_session_store()
    prev = TurnSnapshot(
        session_id="sid-1",
        summary="上一轮：阿尔托莉雅；命中 1 条",
        turn_type="MAJOR",
    )
    store.save_turn(prev)

    captured: dict[str, object] = {}

    async def fake_chat_completion(*, system_prompt: str, **_kwargs):
        captured["system_prompt"] = system_prompt
        return {
            "pipeline": "A",
            "confidence": 0.9,
            "turn_type": "MINOR",
            "_model": "fake-cls",
            "_usage": {"total_tokens": 20},
        }

    state = PipelineState(
        user_message="再帮我看看宝具",
        trace_id="t-mt-1",
        session_id="sid-1",
    )
    state.extras["session_store"] = store

    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    # prev_turn 应被加载到 extras 供下游 merge_filters 节点使用
    assert out.extras.get("prev_turn") is prev
    # prev_summary 应注入到分类器 prompt（验证多轮上下文已传递）
    assert "上一轮" in captured["system_prompt"]
    assert out.turn_type == "MINOR"
    # MINOR 不应清 session
    assert store.load_prev_turn("sid-1") is prev


@pytest.mark.asyncio
async def test_classify_node_major_turn_clears_session_and_drops_prev_turn():
    store = _make_session_store()
    prev = TurnSnapshot(session_id="sid-2", summary="历史摘要", turn_type="MAJOR")
    store.save_turn(prev)
    store.save_pending("sid-2", {"some": "pending"})

    async def fake_chat_completion(**_kwargs):
        return {
            "pipeline": "A",
            "confidence": 0.95,
            "turn_type": "MAJOR",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="换个话题：查梅林",
        trace_id="t-mt-2",
        session_id="sid-2",
    )
    state.extras["session_store"] = store

    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    assert out.turn_type == "MAJOR"
    # MAJOR：必须清空 session，避免标记位残留污染下一轮
    assert store.load_prev_turn("sid-2") is None
    assert store.load_pending("sid-2") is None
    # extras 中也应清掉 prev_turn
    assert "prev_turn" not in out.extras


@pytest.mark.asyncio
async def test_classify_node_no_prev_turn_minor_is_coerced_to_major():
    """没有 prev_turn 上下文时，LLM 输出 MINOR/CORRECTION 视为漂移，必须强制改回 MAJOR。"""
    store = _make_session_store()  # 不预存任何 prev_turn

    async def fake_chat_completion(**_kwargs):
        return {
            "pipeline": "A",
            "confidence": 0.9,
            "turn_type": "MINOR",  # LLM 漂移：没有上下文也判定为 MINOR
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="再筛选一下",
        trace_id="t-mt-3",
        session_id="sid-3",
    )
    state.extras["session_store"] = store

    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    assert out.turn_type == "MAJOR"
    assert "prev_turn" not in out.extras


@pytest.mark.asyncio
async def test_classify_node_without_session_id_skips_session_logic():
    """session_id 为空 → 完全旁路多轮逻辑，行为应与单轮等价。"""
    store = _make_session_store()
    # 即使预存了 session_id 不匹配的 turn 也不应被读取
    store.save_turn(TurnSnapshot(session_id="other", summary="不应被加载"))

    async def fake_chat_completion(**_kwargs):
        return {
            "pipeline": "B",
            "confidence": 0.99,
            "turn_type": "MAJOR",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="梅林何时复刻",
        trace_id="t-mt-4",
        session_id="",
    )
    state.extras["session_store"] = store

    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    assert out.classified_pipeline == "B"
    assert "prev_turn" not in out.extras


@pytest.mark.asyncio
async def test_classify_node_invalid_turn_type_falls_back_to_major():
    """LLM 输出协议外 turn_type 时，必须强制改回 MAJOR。"""
    store = _make_session_store()
    prev = TurnSnapshot(session_id="sid-5", summary="历史")
    store.save_turn(prev)

    async def fake_chat_completion(**_kwargs):
        return {
            "pipeline": "A",
            "confidence": 0.9,
            "turn_type": "REPLACE_ALL",  # 协议外
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="x",
        trace_id="t-mt-5",
        session_id="sid-5",
    )
    state.extras["session_store"] = store

    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    assert out.turn_type == "MAJOR"


# ────────────────────────────────────────────────────────────
# edges — MINOR/CORRECTION 路由分支
# ────────────────────────────────────────────────────────────


def test_after_classify_minor_with_prev_turn_routes_to_merge_filters():
    state = PipelineState(
        classified_pipeline="A",
        classifier_confidence=0.9,
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = TurnSnapshot(session_id="sid", summary="x")
    assert after_classify(state) == "merge_filters"


def test_after_classify_correction_with_prev_turn_routes_to_merge_filters():
    state = PipelineState(
        classified_pipeline="A",
        classifier_confidence=0.9,
        turn_type="CORRECTION",
    )
    state.extras["prev_turn"] = TurnSnapshot(session_id="sid", summary="x")
    assert after_classify(state) == "merge_filters"


def test_after_classify_minor_without_prev_turn_falls_back_to_route():
    """缺 prev_turn 时即使 turn_type=MINOR 也必须走 route，避免空合并。"""
    state = PipelineState(
        classified_pipeline="A",
        classifier_confidence=0.9,
        turn_type="MINOR",
    )
    # 故意不放 prev_turn
    assert after_classify(state) == "route"


def test_after_classify_major_with_prev_turn_still_goes_to_route():
    """有 prev_turn 但 turn_type=MAJOR 应走标准 route，不合并。"""
    state = PipelineState(
        classified_pipeline="A",
        classifier_confidence=0.9,
        turn_type="MAJOR",
    )
    state.extras["prev_turn"] = TurnSnapshot(session_id="sid", summary="x")
    assert after_classify(state) == "route"


def test_after_classify_pipeline_b_ignores_turn_type():
    """B 链路无视 turn_type，永远走 atlas。"""
    state = PipelineState(
        classified_pipeline="B",
        classifier_confidence=0.9,
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = TurnSnapshot(session_id="sid", summary="x")
    assert after_classify(state) == "atlas"


# ────────────────────────────────────────────────────────────
# edges — after_merge_filters
# ────────────────────────────────────────────────────────────


def test_after_merge_filters_success_proceeds_to_execute():
    state = PipelineState()
    assert after_merge_filters(state) == "execute"


def test_after_merge_filters_merge_failed_falls_back_to_route():
    state = PipelineState()
    state.extras["bail_out"] = "merge_failed_fallback_route"
    assert after_merge_filters(state) == "route"
    # 失败专用 bail_out 应被消费（不残留给后续节点误判）
    assert "bail_out" not in state.extras


def test_after_merge_filters_other_bail_out_uses_standard_dispatch():
    state = PipelineState()
    state.extras["bail_out"] = "clarification"
    assert after_merge_filters(state) == "clarify"


# ────────────────────────────────────────────────────────────
# generate_node — save_turn 行为
# ────────────────────────────────────────────────────────────


def _make_execution_result(servants=None, total_found=0):
    from server.skills.executor import ExecutionResult

    return ExecutionResult(
        servants=servants or [],
        total_found=total_found,
        response_skill=None,
        is_fallback=False,
        accepted_skills=[],
        rejected_skills=[],
        execution_time_ms=1.0,
        clarification=None,
        custom_context=None,
    )


@pytest.mark.asyncio
async def test_generate_node_writes_turn_snapshot_when_session_store_present():
    from server.nodes.generate import generate_node

    store = _make_session_store()
    fake_result = _make_execution_result(
        servants=[{"id": 1, "collectionNo": 100, "name": "梅林", "className": "caster"}],
        total_found=1,
    )

    async def fake_chat_completion(**_kwargs):
        return {"text": "梅林是 caster。", "_usage": {}, "_model": "gen"}

    state = PipelineState(
        user_message="查梅林",
        trace_id="t-gen-mt",
        session_id="sid-save",
        skill_calls=[{"skill_name": "search_servants", "params": {"name": "梅林"}}],
        response_skill_name="respond_servant_list",
        classified_pipeline="A",
        turn_type="MAJOR",
    )
    state.extras["executor_result"] = fake_result
    state.extras["session_store"] = store

    with patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion):
        out = await generate_node(state)

    assert out.reply == "梅林是 caster。"
    # 应写入 SessionStore 的 turn 命名空间
    snap = store.load_prev_turn("sid-save")
    assert snap is not None
    assert snap.session_id == "sid-save"
    assert snap.user_message == "查梅林"
    assert snap.turn_type == "MAJOR"
    assert snap.skill_calls == state.skill_calls
    assert snap.response_skill_name == "respond_servant_list"
    # servants 仅保留 collectionNo + name，避免快照膨胀
    assert snap.servants == [{"collectionNo": 100, "name": "梅林"}]
    # summary 包含本轮筛选 / 命中数 / 回复要点
    assert "命中 1 条" in snap.summary


@pytest.mark.asyncio
async def test_generate_node_without_session_id_skips_save_turn():
    from server.nodes.generate import generate_node

    store = _make_session_store()
    fake_result = _make_execution_result(servants=[{"id": 1, "name": "X"}], total_found=1)

    async def fake_chat_completion(**_kwargs):
        return {"text": "回复", "_usage": {}, "_model": "gen"}

    state = PipelineState(
        user_message="any",
        trace_id="t-gen-nosid",
        session_id="",  # 空 session_id → 不应写 SessionStore
        skill_calls=[{"skill_name": "search_servants", "params": {}}],
        response_skill_name="respond_servant_list",
    )
    state.extras["executor_result"] = fake_result
    state.extras["session_store"] = store

    with patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion):
        await generate_node(state)

    # 没有 session_id 不应有任何写入
    assert store.load_prev_turn("") is None


@pytest.mark.asyncio
async def test_generate_node_save_turn_failure_does_not_break_main_flow():
    """SessionStore.save_turn 抛错时 generate_node 仍应正常返回 reply。"""
    from server.nodes.generate import generate_node

    class FailingStore:
        def save_turn(self, _snap):
            raise RuntimeError("disk full")

    fake_result = _make_execution_result(servants=[], total_found=0)

    async def fake_chat_completion(**_kwargs):
        return {"text": "OK", "_usage": {}, "_model": "gen"}

    state = PipelineState(
        user_message="any",
        trace_id="t-gen-savefail",
        session_id="sid-fail",
        skill_calls=[],
        response_skill_name="respond_servant_list",
    )
    state.extras["executor_result"] = fake_result
    state.extras["session_store"] = FailingStore()

    with patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion):
        out = await generate_node(state)

    # 主流程不受影响
    assert out.reply == "OK"
