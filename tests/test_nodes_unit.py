"""nodes/{classify,route,execute,generate}.py 单元测试（Task 2 — ADR-028）。

每个节点的输入/输出契约：
- classify_node：成功路径 + 2 次重试失败降级（A + 1.0）
- route_node：成功 / clarification / fallback / empty_skill_calls / 重试失败 5 路 bail_out
- execute_node：成功 / clarification / fallback / 昵称识别恢复主路径
- generate_node：成功 / 缺 executor_result 防御 / chat_completion 失败降级模板
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.graph.state import PipelineState

# ────────────────────────────────────────────────────────────
# classify_node
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_node_success_writes_pipeline_and_confidence():
    from server.nodes.classify import classify_node

    async def fake_chat_completion(**_kwargs):
        return {
            "pipeline": "B",
            "confidence": 0.92,
            "_model": "fake-classifier",
            "_usage": {"total_tokens": 50},
        }

    state = PipelineState(user_message="梅林什么时候复刻", trace_id="t-cls-1")
    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    assert out is state  # 原地修改
    assert out.classified_pipeline == "B"
    assert out.classifier_confidence == 0.92
    assert out.classifier_model == "fake-classifier"
    assert out.trace_total_tokens == 50


@pytest.mark.asyncio
async def test_classify_node_two_retries_fail_falls_back_to_a():
    from server.nodes.classify import classify_node

    async def fake_chat_completion(**_kwargs):
        raise RuntimeError("LLM 503")

    state = PipelineState(user_message="任意输入", trace_id="t-cls-2")
    with patch("server.nodes.classify.chat_completion", side_effect=fake_chat_completion):
        out = await classify_node(state)

    # 降级：A + 1.0 → after_classify 仍走 route
    assert out.classified_pipeline == "A"
    assert out.classifier_confidence == 1.0
    assert out.classifier_model == "unknown"


# ────────────────────────────────────────────────────────────
# route_node
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_node_success_writes_skill_calls():
    from server.nodes.route import route_node

    async def fake_chat_completion(**_kwargs):
        return {
            "skill_calls": [{"skill_name": "search_servants", "params": {"name": "梅林"}}],
            "response_skill": "respond_servant_list",
            "fallback": None,
            "clarification": None,
            "target_pipeline": "A",
            "_model": "fake-route",
            "_usage": {"total_tokens": 80},
        }

    state = PipelineState(user_message="查询梅林", trace_id="t-route-1")
    with patch("server.nodes.route.chat_completion", side_effect=fake_chat_completion):
        out = await route_node(state)

    assert out.skill_calls == [{"skill_name": "search_servants", "params": {"name": "梅林"}}]
    assert out.response_skill_name == "respond_servant_list"
    assert out.target_pipeline == "A"
    assert out.model_used == "fake-route"
    assert "bail_out" not in out.extras


@pytest.mark.asyncio
async def test_route_node_clarification_bails_out():
    from server.nodes.route import route_node

    async def fake_chat_completion(**_kwargs):
        return {
            "skill_calls": [],
            "response_skill": "respond_servant_list",
            "fallback": None,
            "clarification": {"question": "你说的是哪个伊织？"},
            "_model": "m",
            "_usage": {},
        }

    state = PipelineState(user_message="查伊织", trace_id="t-route-2")
    with patch("server.nodes.route.chat_completion", side_effect=fake_chat_completion):
        out = await route_node(state)

    assert out.extras["bail_out"] == "clarification"
    assert out.extras["routing_result"]["clarification"] == {"question": "你说的是哪个伊织？"}


@pytest.mark.asyncio
async def test_route_node_fallback_bails_out_with_code_prefix():
    from server.nodes.route import route_node

    async def fake_chat_completion(**_kwargs):
        return {
            "skill_calls": [],
            "response_skill": "respond_servant_list",
            "fallback": {"code": "greeting", "message": "你好"},
            "clarification": None,
            "_model": "m",
            "_usage": {},
        }

    state = PipelineState(user_message="你好", trace_id="t-route-3")
    with patch("server.nodes.route.chat_completion", side_effect=fake_chat_completion):
        out = await route_node(state)

    assert out.extras["bail_out"] == "fallback_greeting"


@pytest.mark.asyncio
async def test_route_node_empty_skill_calls_bails_out():
    from server.nodes.route import route_node

    async def fake_chat_completion(**_kwargs):
        return {
            "skill_calls": [],
            "response_skill": "respond_servant_list",
            "fallback": None,
            "clarification": None,
            "_model": "m",
            "_usage": {},
        }

    state = PipelineState(user_message="???", trace_id="t-route-4")
    with patch("server.nodes.route.chat_completion", side_effect=fake_chat_completion):
        out = await route_node(state)

    assert out.extras["bail_out"] == "empty_skill_calls"


@pytest.mark.asyncio
async def test_route_node_two_retries_fail_bails_out():
    from server.nodes.route import route_node

    async def fake_chat_completion(**_kwargs):
        raise RuntimeError("LLM 503")

    state = PipelineState(user_message="any", trace_id="t-route-5")
    with patch("server.nodes.route.chat_completion", side_effect=fake_chat_completion):
        out = await route_node(state)

    assert out.extras["bail_out"] == "routing_failed"
    assert "LLM 503" in out.extras["routing_error"]


# ────────────────────────────────────────────────────────────
# execute_node
# ────────────────────────────────────────────────────────────


def _make_execution_result(
    *,
    servants=None,
    total_found=0,
    is_fallback=False,
    clarification=None,
    custom_context=None,
):
    """构造一个最小可用的 ExecutionResult mock。"""
    from server.skills.executor import ExecutionResult

    return ExecutionResult(
        servants=servants or [],
        total_found=total_found,
        response_skill=None,
        is_fallback=is_fallback,
        accepted_skills=[],
        rejected_skills=[],
        execution_time_ms=1.0,
        clarification=clarification,
        custom_context=custom_context,
    )


@pytest.mark.asyncio
async def test_execute_node_success_writes_servants_and_count():
    from server.nodes.execute import execute_node

    fake_result = _make_execution_result(
        servants=[{"id": 1, "name": "梅林"}],
        total_found=1,
    )

    fake_executor = MagicMock()
    fake_executor.execute = MagicMock(return_value=fake_result)

    state = PipelineState(
        user_message="查梅林",
        trace_id="t-exec-1",
        skill_calls=[{"skill_name": "search_servants", "params": {"name": "梅林"}}],
    )
    with patch("server.nodes.execute.SkillExecutor", return_value=fake_executor):
        out = await execute_node(state)

    assert out.servants == [{"id": 1, "name": "梅林"}]
    assert out.count == 1
    assert "bail_out" not in out.extras
    assert out.extras["executor_result"] is fake_result


@pytest.mark.asyncio
async def test_execute_node_clarification_bails_out():
    from server.nodes.execute import execute_node

    # 非 empty-name 类型的 clarification 直接 bail_out（不触发 guess_candidates_async）
    fake_result = _make_execution_result(
        clarification={"type": "multi_candidate", "candidates": ["A", "B"]},
    )
    fake_executor = MagicMock()
    fake_executor.execute = MagicMock(return_value=fake_result)
    fake_executor.guess_candidates_async = AsyncMock(return_value=fake_result)

    state = PipelineState(user_message="查伊织", trace_id="t-exec-2", skill_calls=[{"x": 1}])
    with patch("server.nodes.execute.SkillExecutor", return_value=fake_executor):
        out = await execute_node(state)

    assert out.extras["bail_out"] == "execution_clarification"
    fake_executor.guess_candidates_async.assert_not_called()


@pytest.mark.asyncio
async def test_execute_node_fallback_with_nickname_resolved_main_path():
    """执行 fallback → 异步昵称识别成功 → 主路径成功。"""
    from server.nodes.execute import execute_node

    initial_result = _make_execution_result(is_fallback=True, total_found=0)
    resolved_result = _make_execution_result(
        servants=[{"id": 100, "name": "BB"}],
        total_found=1,
        is_fallback=False,
    )

    fake_executor = MagicMock()
    fake_executor.execute = MagicMock(return_value=initial_result)
    fake_executor.try_resolve_nickname_async = AsyncMock(return_value=resolved_result)

    state = PipelineState(
        user_message="查BB",
        trace_id="t-exec-3",
        skill_calls=[{"skill_name": "search_servants", "params": {"name": "BB"}}],
    )
    with patch("server.nodes.execute.SkillExecutor", return_value=fake_executor):
        out = await execute_node(state)

    assert "bail_out" not in out.extras
    assert out.servants == [{"id": 100, "name": "BB"}]
    assert out.count == 1


@pytest.mark.asyncio
async def test_execute_node_fallback_unrecoverable_bails_out():
    from server.nodes.execute import execute_node

    initial_result = _make_execution_result(is_fallback=True)

    fake_executor = MagicMock()
    fake_executor.execute = MagicMock(return_value=initial_result)
    fake_executor.try_resolve_nickname_async = AsyncMock(return_value=initial_result)

    state = PipelineState(user_message="???", trace_id="t-exec-4", skill_calls=[{"x": 1}])
    with patch("server.nodes.execute.SkillExecutor", return_value=fake_executor):
        out = await execute_node(state)

    assert out.extras["bail_out"] == "execution_fallback"


# ────────────────────────────────────────────────────────────
# generate_node
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_node_success_writes_reply():
    from server.nodes.generate import generate_node

    fake_result = _make_execution_result(
        servants=[{"id": 1, "name": "梅林", "className": "caster"}],
        total_found=1,
    )

    async def fake_chat_completion(**_kwargs):
        return {"text": "梅林是 caster。", "_usage": {"total_tokens": 30}, "_model": "gen"}

    state = PipelineState(
        user_message="查梅林",
        trace_id="t-gen-1",
        skill_calls=[{"skill_name": "search_servants", "params": {"name": "梅林"}}],
        response_skill_name="respond_servant_list",
    )
    state.extras["executor_result"] = fake_result

    with patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion):
        out = await generate_node(state)

    assert out.reply == "梅林是 caster。"
    assert out.count == 1
    assert out.servants == [{"id": 1, "name": "梅林", "className": "caster"}]
    assert out.query == {
        "mode": "skill",
        "skill_calls": [{"skill_name": "search_servants", "params": {"name": "梅林"}}],
    }


@pytest.mark.asyncio
async def test_generate_node_no_executor_result_returns_safe_default():
    from server.nodes.generate import generate_node

    state = PipelineState(user_message="任意", trace_id="t-gen-2")
    out = await generate_node(state)

    assert "暂时无法生成回复" in out.reply
    assert out.query == {"mode": "skill", "skill_calls": []}


@pytest.mark.asyncio
async def test_generate_node_chat_completion_fail_falls_back_to_template():
    from server.nodes.generate import generate_node

    fake_result = _make_execution_result(
        servants=[{"id": 1, "name": "X"}],
        total_found=3,
    )

    async def fake_chat_completion(**_kwargs):
        raise RuntimeError("LLM down")

    state = PipelineState(
        user_message="any",
        trace_id="t-gen-3",
        skill_calls=[{"skill_name": "search_servants", "params": {}}],
        response_skill_name="respond_servant_list",
    )
    state.extras["executor_result"] = fake_result

    with patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion):
        out = await generate_node(state)

    assert "为你找到了 3 位从者" in out.reply
    assert out.count == 3


@pytest.mark.asyncio
async def test_generate_node_ce_list_template_fallback():
    from server.nodes.generate import generate_node

    fake_result = _make_execution_result(
        servants=[{"id": 9001, "name": "黑杯"}],
        total_found=2,
    )

    async def fake_chat_completion(**_kwargs):
        raise RuntimeError("LLM 500")

    state = PipelineState(
        user_message="any",
        trace_id="t-gen-4",
        skill_calls=[{"skill_name": "search_ces", "params": {}}],
        response_skill_name="respond_ce_list",
    )
    state.extras["executor_result"] = fake_result

    with patch("server.nodes.generate.chat_completion", side_effect=fake_chat_completion):
        out = await generate_node(state)

    assert "为你找到了 2 个礼装" in out.reply
