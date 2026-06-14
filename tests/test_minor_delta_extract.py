"""merge_filters_node 单元测试（Task 4 Batch B — ADR-028）。

覆盖 ``server/nodes/merge_filters.py`` 的 4 种 delta op 和降级路径：
- G1 reuse：完全复用 prev skill_calls / response_skill
- G2 append_filters：去重追加（避免 LLM 重复 prev 调用）
- G3 switch_response：仅切换 response_skill；缺 response_skill 必须降级
- G4 patch_params：CORRECTION 时合并参数；无命中补丁必须降级
- 前置防御：缺 prev_turn / turn_type 不合法 → 降级
- LLM 调用失败 → 2 次重试后降级
- 未知 op → 降级
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from server.graph.session import TurnSnapshot
from server.graph.state import PipelineState
from server.nodes.merge_filters import _params_key, merge_filters_node


def _make_prev_turn(
    skill_calls: list[dict] | None = None,
    response_skill_name: str = "respond_servant_list",
    summary: str = "上一轮：阿尔托莉雅；命中 1 条",
) -> TurnSnapshot:
    return TurnSnapshot(
        session_id="sid-x",
        user_message="阿尔托莉雅是什么职阶",
        reply="...",
        summary=summary,
        pipeline="A",
        skill_calls=skill_calls or [{"skill_name": "lookup_servant", "params": {"name": "阿尔托莉雅"}}],
        response_skill_name=response_skill_name,
        servants=[{"collectionNo": 100, "name": "Saber"}],
        query={},
        turn_type="MAJOR",
        timestamp=1.0,
    )


# ────────────────────────────────────────────────────────────
# G1 reuse — 完全复用 prev
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_filters_op_reuse_keeps_prev_calls():
    prev = _make_prev_turn()

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "reuse",
            "skill_calls": [],
            "response_skill": None,
            "patches": [],
            "rationale": "用户只是换种问法",
            "_model": "fake",
            "_usage": {"total_tokens": 30},
        }

    state = PipelineState(
        user_message="再帮我看看她的宝具",
        trace_id="t-merge-reuse",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert out.skill_calls == prev.skill_calls
    assert out.response_skill_name == prev.response_skill_name
    assert out.target_pipeline == "A"
    assert "bail_out" not in out.extras
    assert out.extras["routing_result"]["source"] == "minor_merge"


# ────────────────────────────────────────────────────────────
# G2 append_filters — 追加 + 去重
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_filters_op_append_filters_dedup_against_prev():
    prev = _make_prev_turn(
        skill_calls=[{"skill_name": "search_by_class", "params": {"className": "saber"}}],
    )

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "append_filters",
            "skill_calls": [
                # 与 prev 完全重复 → 应被去重
                {"skill_name": "search_by_class", "params": {"className": "saber"}},
                # 新条件 → 应追加
                {"skill_name": "search_by_rarity", "params": {"rarity": 5}},
            ],
            "response_skill": None,
            "patches": [],
            "rationale": "其中五星的",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="其中五星的",
        trace_id="t-merge-append",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    # 应保留 prev 的 1 条 + 追加 1 条新 = 2 条
    assert len(out.skill_calls) == 2
    assert out.skill_calls[0]["skill_name"] == "search_by_class"
    assert out.skill_calls[1] == {"skill_name": "search_by_rarity", "params": {"rarity": 5}}
    # response_skill 未指定 → 沿用 prev
    assert out.response_skill_name == prev.response_skill_name
    assert "bail_out" not in out.extras


# ────────────────────────────────────────────────────────────
# G3 switch_response — 仅切换响应技能；缺失必须降级
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_filters_op_switch_response_keeps_calls_changes_response_skill():
    prev = _make_prev_turn()

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "switch_response",
            "skill_calls": [],
            "response_skill": "respond_servant_detail",
            "patches": [],
            "rationale": "详细说说",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="详细说说",
        trace_id="t-merge-switch",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert out.skill_calls == prev.skill_calls  # 未改
    assert out.response_skill_name == "respond_servant_detail"
    assert "bail_out" not in out.extras


@pytest.mark.asyncio
async def test_merge_filters_op_switch_response_missing_skill_falls_back():
    prev = _make_prev_turn()

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "switch_response",
            "skill_calls": [],
            "response_skill": None,  # 协议要求必填，缺失 → 降级
            "patches": [],
            "rationale": "",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="详细",
        trace_id="t-merge-switch-bad",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert out.turn_type == "MAJOR"
    assert out.extras["bail_out"] == "merge_failed_fallback_route"
    assert "prev_turn" not in out.extras


# ────────────────────────────────────────────────────────────
# G4 patch_params — CORRECTION 合并参数；无命中必须降级
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_filters_op_patch_params_updates_prev_call():
    prev = _make_prev_turn(
        skill_calls=[{"skill_name": "lookup_servant", "params": {"name": "阿尔托莉雅"}}],
    )

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "patch_params",
            "skill_calls": [],
            "response_skill": None,
            "patches": [{"skill_name": "lookup_servant", "params": {"name": "阿尔托莉雅Alter"}}],
            "rationale": "用户更正名字",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="我说的是Alter版",
        trace_id="t-merge-patch",
        session_id="sid-x",
        turn_type="CORRECTION",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert len(out.skill_calls) == 1
    assert out.skill_calls[0]["params"]["name"] == "阿尔托莉雅Alter"
    # 不应改写 prev_turn 中的原数据（深拷贝保证）
    assert prev.skill_calls[0]["params"]["name"] == "阿尔托莉雅"
    assert "bail_out" not in out.extras


@pytest.mark.asyncio
async def test_merge_filters_op_patch_params_no_target_falls_back():
    prev = _make_prev_turn(
        skill_calls=[{"skill_name": "lookup_servant", "params": {"name": "梅林"}}],
    )

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "patch_params",
            "skill_calls": [],
            "response_skill": None,
            # patch 的 skill_name 在 prev 中根本不存在 → 0 命中 → 降级
            "patches": [{"skill_name": "search_by_class", "params": {"className": "caster"}}],
            "rationale": "",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="我说的是 Caster 阶的",
        trace_id="t-merge-patch-bad",
        session_id="sid-x",
        turn_type="CORRECTION",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert out.turn_type == "MAJOR"
    assert out.extras["bail_out"] == "merge_failed_fallback_route"
    assert "prev_turn" not in out.extras


# ────────────────────────────────────────────────────────────
# 前置防御 / LLM 失败 / 未知 op
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_filters_no_prev_turn_falls_back_to_major():
    state = PipelineState(
        user_message="再说说",
        trace_id="t-merge-noprev",
        session_id="sid-x",
        turn_type="MINOR",
    )
    # 故意不设 prev_turn

    out = await merge_filters_node(state)

    assert out.turn_type == "MAJOR"
    assert out.extras["bail_out"] == "merge_failed_fallback_route"


@pytest.mark.asyncio
async def test_merge_filters_invalid_turn_type_falls_back():
    prev = _make_prev_turn()
    state = PipelineState(
        user_message="x",
        trace_id="t-merge-invalid-type",
        session_id="sid-x",
        turn_type="MAJOR",  # 不在 (MINOR, CORRECTION) → 直接降级
    )
    state.extras["prev_turn"] = prev

    out = await merge_filters_node(state)

    assert out.turn_type == "MAJOR"
    assert out.extras["bail_out"] == "merge_failed_fallback_route"
    assert "prev_turn" not in out.extras


@pytest.mark.asyncio
async def test_merge_filters_llm_two_retries_fail_falls_back():
    prev = _make_prev_turn()

    async def fake_chat_completion(**_kwargs):
        raise RuntimeError("LLM 503")

    state = PipelineState(
        user_message="再筛选一下",
        trace_id="t-merge-llm-fail",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert out.turn_type == "MAJOR"
    assert out.extras["bail_out"] == "merge_failed_fallback_route"
    assert "prev_turn" not in out.extras


@pytest.mark.asyncio
async def test_merge_filters_unknown_op_falls_back():
    prev = _make_prev_turn()

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "drop_filters",  # 协议外 op
            "skill_calls": [],
            "response_skill": None,
            "patches": [],
            "rationale": "",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="x",
        trace_id="t-merge-unknown-op",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    assert out.turn_type == "MAJOR"
    assert out.extras["bail_out"] == "merge_failed_fallback_route"


# ────────────────────────────────────────────────────────────
# _params_key 工具函数
# ────────────────────────────────────────────────────────────


def test_params_key_empty_dict():
    assert _params_key({}) == ""


def test_params_key_stable_order():
    # 不同插入顺序应生成相同 key
    k1 = _params_key({"b": 2, "a": 1})
    k2 = _params_key({"a": 1, "b": 2})
    assert k1 == k2
    assert k1 == "a=1|b=2"


def test_params_key_non_dict_returns_empty():
    assert _params_key("not a dict") == ""  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────
# Streaming SSE — MINOR 追问下推送筛选条件 thinking 事件
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_filters_streaming_pushes_routed_event_on_append():
    """streaming=True 且 op=append_filters 时，应推送 phase=routed 的 thinking 事件，
    detail 包含合并后的全部筛选条件中文描述（覆盖『其中弓阶的』场景）。"""
    prev = _make_prev_turn(
        skill_calls=[
            {"skill_name": "search_by_skill_effect", "params": {"effect": "NP增加"}},
        ],
    )

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "append_filters",
            "skill_calls": [
                {"skill_name": "search_by_class", "params": {"className": "archer"}},
            ],
            "response_skill": None,
            "patches": [],
            "rationale": "其中弓阶的",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="其中弓阶的",
        trace_id="t-merge-stream-append",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev
    state.extras["streaming"] = True  # 开启流式

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    # 至少推送了 1 个 thinking 事件
    routed_events = [
        e for e in out.pending_events if e.get("type") == "thinking" and e.get("data", {}).get("phase") == "routed"
    ]
    assert len(routed_events) == 1
    payload = routed_events[0]["data"]
    assert payload["message"] == "已在上一轮基础上追加筛选"
    # detail 应包含合并后的两个筛选条件（描述顺序由 describe_filters 决定）
    assert payload["detail"]
    assert "弓阶" in payload["detail"] or "Archer" in payload["detail"]


@pytest.mark.asyncio
async def test_merge_filters_no_streaming_no_thinking_event():
    """非 streaming 模式不应推送 thinking 事件，避免污染 pending_events。"""
    prev = _make_prev_turn()

    async def fake_chat_completion(**_kwargs):
        return {
            "op": "reuse",
            "skill_calls": [],
            "response_skill": None,
            "patches": [],
            "rationale": "",
            "_model": "fake",
            "_usage": {},
        }

    state = PipelineState(
        user_message="再看看",
        trace_id="t-merge-no-stream",
        session_id="sid-x",
        turn_type="MINOR",
    )
    state.extras["prev_turn"] = prev
    # 不设置 streaming

    with patch("server.nodes.merge_filters.chat_completion", side_effect=fake_chat_completion):
        out = await merge_filters_node(state)

    routed_events = [
        e for e in out.pending_events if e.get("type") == "thinking" and e.get("data", {}).get("phase") == "routed"
    ]
    assert routed_events == []
