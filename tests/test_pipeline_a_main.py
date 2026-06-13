"""Pipeline A 端到端测试（Task 2 — ADR-028）。

通过 mock 全链路 LLM + SkillExecutor，从 ``handle_skill_mode`` 入口跑通：
- 直传 skill_calls 路径（preset / 前端直传）
- 完整 A 图（Stage 0 → Stage 1 → execute → generate）
- bail_out 走 _bail_out_to_chat_response 的 clarification / fallback 模板分支

不涉及 Task 3 的 Agent 兜底（low_confidence_agent / routing_failed / no_match / execution_fallback）
— 那部分留给 Task 3 的回归测试覆盖。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_executor_mock(**kwargs):
    """构造 SkillExecutor 实例 mock。kwargs 转发到 ExecutionResult。"""
    from server.skills.executor import ExecutionResult

    result = ExecutionResult(
        servants=kwargs.get("servants", []),
        total_found=kwargs.get("total_found", 0),
        response_skill=None,
        is_fallback=kwargs.get("is_fallback", False),
        accepted_skills=kwargs.get("accepted_skills", []),
        rejected_skills=kwargs.get("rejected_skills", []),
        execution_time_ms=1.0,
        clarification=kwargs.get("clarification"),
        custom_context=kwargs.get("custom_context"),
    )
    executor = MagicMock()
    executor.execute = MagicMock(return_value=result)
    executor.guess_candidates_async = AsyncMock(return_value=result)
    executor.try_resolve_nickname_async = AsyncMock(return_value=result)
    return executor, result


# ────────────────────────────────────────────────────────────
# 直传 skill_calls 短图（execute → generate）
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_skill_mode_direct_skill_calls_success():
    """前端直传 skill_calls：跳过 classify/route，直接 execute → generate。"""
    from server import pipeline

    executor, _ = _make_executor_mock(
        servants=[{"id": 1, "name": "梅林", "className": "caster"}],
        total_found=1,
    )

    async def fake_chat_completion(**_kwargs):
        return {
            "text": "梅林是 caster 职阶。",
            "_usage": {"total_tokens": 40},
            "_model": "fake-gen",
        }

    with (
        patch("server.nodes.execute.SkillExecutor", return_value=executor),
        patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion),
    ):
        resp = await pipeline.handle_skill_mode(
            user_message="查询梅林",
            trace_id="t-direct-1",
            skill_calls=[{"skill_name": "search_servants", "params": {"name": "梅林"}}],
            response_skill_name="respond_servant_list",
        )

    assert resp.reply == "梅林是 caster 职阶。"
    assert resp.count == 1
    assert resp.servants == [{"id": 1, "name": "梅林", "className": "caster"}]
    assert resp.traceId == "t-direct-1"
    assert resp.query["mode"] == "skill"


# ────────────────────────────────────────────────────────────
# 完整 A 图：classify → route → execute → generate
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_skill_mode_full_pipeline_a_success():
    from server import pipeline

    async def fake_classify(**_kwargs):
        return {
            "pipeline": "A",
            "confidence": 0.95,
            "_model": "cls-m",
            "_usage": {"total_tokens": 20},
        }

    async def fake_route(**_kwargs):
        return {
            "skill_calls": [{"skill_name": "search_servants", "params": {"name": "梅林"}}],
            "response_skill": "respond_servant_list",
            "fallback": None,
            "clarification": None,
            "target_pipeline": "A",
            "_model": "route-m",
            "_usage": {"total_tokens": 60},
        }

    async def fake_gen(**_kwargs):
        return {"text": "梅林是 5 星 caster。", "_usage": {"total_tokens": 50}, "_model": "gen-m"}

    executor, _ = _make_executor_mock(
        servants=[{"id": 1, "name": "梅林", "className": "caster"}],
        total_found=1,
    )

    with (
        patch("server.nodes.classify.chat_completion", side_effect=fake_classify),
        patch("server.nodes.route.chat_completion", side_effect=fake_route),
        patch("server.nodes.execute.SkillExecutor", return_value=executor),
        patch("server.nodes.generate.chat_completion", side_effect=fake_gen),
    ):
        resp = await pipeline.handle_skill_mode(
            user_message="查询梅林",
            trace_id="t-full-1",
        )

    assert resp.reply == "梅林是 5 星 caster。"
    assert resp.count == 1
    assert resp.model == "gen-m" or resp.model == "route-m"
    # 注意：state.model_used 在 route_node 中被覆盖为 route-m，generate 不会再变
    assert resp.traceId == "t-full-1"


# ────────────────────────────────────────────────────────────
# bail_out 路径：fallback_greeting → 模板回复
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_skill_mode_fallback_greeting_returns_template():
    """A 链路高置信度但 routing 返回 fallback.code=greeting → 模板回复。"""
    from server import pipeline

    async def fake_classify(**_kwargs):
        return {"pipeline": "A", "confidence": 0.95, "_model": "m", "_usage": {}}

    async def fake_route(**_kwargs):
        return {
            "skill_calls": [],
            "response_skill": "respond_servant_list",
            "fallback": {"code": "greeting", "message": "你好"},
            "clarification": None,
            "_model": "route-m",
            "_usage": {},
        }

    with (
        patch("server.nodes.classify.chat_completion", side_effect=fake_classify),
        patch("server.nodes.route.chat_completion", side_effect=fake_route),
    ):
        resp = await pipeline.handle_skill_mode(
            user_message="你好",
            trace_id="t-greet",
        )

    # FALLBACK_TEMPLATES 中应有 GREETING 模板；即便 fallback 自带 message，模板优先
    assert resp.reply  # 非空
    assert resp.count == 0
    assert resp.servants == []


# ────────────────────────────────────────────────────────────
# bail_out 路径：clarification → query.mode = "clarification"
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_skill_mode_routing_clarification_returns_clarification_query():
    from server import pipeline

    async def fake_classify(**_kwargs):
        return {"pipeline": "A", "confidence": 0.95, "_model": "m", "_usage": {}}

    async def fake_route(**_kwargs):
        return {
            "skill_calls": [],
            "response_skill": "respond_servant_list",
            "fallback": None,
            "clarification": {
                "question": "你说的是哪个伊织？",
                "options": ["FGO 伊织", "其他"],
                "ambiguous_field": "name",
            },
            "_model": "route-m",
            "_usage": {},
        }

    with (
        patch("server.nodes.classify.chat_completion", side_effect=fake_classify),
        patch("server.nodes.route.chat_completion", side_effect=fake_route),
    ):
        resp = await pipeline.handle_skill_mode(user_message="查伊织", trace_id="t-clar")

    assert resp.reply == ""
    assert resp.count == 0
    assert resp.query["mode"] == "clarification"
    assert resp.query["clarification"]["question"] == "你说的是哪个伊织？"


# ────────────────────────────────────────────────────────────
# 图实例缓存
# ────────────────────────────────────────────────────────────


def test_pipeline_a_graph_is_cached():
    from server import pipeline

    g1 = pipeline._get_pipeline_a_graph()
    g2 = pipeline._get_pipeline_a_graph()
    assert g1 is g2


def test_pipeline_direct_graph_is_cached():
    from server import pipeline

    g1 = pipeline._get_pipeline_direct_graph()
    g2 = pipeline._get_pipeline_direct_graph()
    assert g1 is g2


def test_pipeline_a_graph_topology():
    from server import pipeline
    from server.graph.engine import END

    g = pipeline._build_pipeline_a_graph()
    assert g._entry == "classify"
    # classify / route / execute 是条件边；atlas / guide / generate 是静态边到 END
    assert "classify" in g._cond_edges
    assert "route" in g._cond_edges
    assert "execute" in g._cond_edges
    assert g._static_edges["atlas"] == END
    assert g._static_edges["guide"] == END
    assert g._static_edges["generate"] == END


def test_pipeline_direct_graph_topology():
    from server import pipeline
    from server.graph.engine import END

    g = pipeline._build_pipeline_direct_graph()
    assert g._entry == "execute"
    assert "execute" in g._cond_edges
    assert g._static_edges["generate"] == END
