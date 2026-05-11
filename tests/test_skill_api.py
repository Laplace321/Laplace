"""
API 集成测试。

测试范围：
- chat() 的路由分发（Agent 路由 / Preset 旁路）
- Preset 路径（preset_name / params 覆盖 / B1 补充解析）
- Agent 路由路径（mock agent_route，含 fallback 分类）
- 未知 preset 错误处理
- 直传 params 路径

使用 FastAPI TestClient + mock agent_route / chat_completion，SkillExecutor 真实执行。
"""

from collections import deque
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import server.skills  # noqa: F401 — 触发 @register_skill 注册
from server.main import app


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """每个测试前重置 Rate Limiter 计数器，避免测试间累积导致 429。"""
    for middleware in app.user_middleware:
        if hasattr(middleware, "cls") and middleware.cls.__name__ == "RateLimitMiddleware":
            break
    # 直接遍历 app.middleware_stack 找到 RateLimitMiddleware 实例并重置
    mw = app.middleware_stack
    while mw is not None:
        if hasattr(mw, "requests") and hasattr(mw, "global_requests"):
            mw.requests.clear()
            mw.global_requests = deque()
            break
        mw = getattr(mw, "app", None)
    yield


@pytest.fixture
def mock_chat_completion_rag():
    """Mock chat_completion 仅用于 RAG 生成阶段（json_mode=False 的调用）。

    返回固定文本回复，避免真实 LLM 调用。
    """

    async def side_effect(**kwargs):
        if kwargs.get("json_mode") is False:
            return {"text": "这是 mock 生成的回复。", "_model": "mock-rag"}
        # 路由阶段的调用（json_mode=True）— 应由各测试自行 mock
        raise ValueError("Unexpected chat_completion call with json_mode=True")

    return side_effect


# ============================================================
# Skill 模式：Preset 路径测试
# ============================================================


class TestSkillModePreset:
    """测试 skill 模式下通过 preset_name 调用。"""

    @pytest.mark.anyio
    async def test_preset_cycle_farming(self, mock_chat_completion_rag):
        """preset=cycle_farming 应直接执行预设 Skills，不调 LLM 路由。"""
        with patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_rag)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={
                        "message": "帮我筛选周回从者",
                        "mode": "skill",
                        "preset_name": "cycle_farming",
                    },
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        assert data["query"]["mode"] == "preset"
        assert data["reply"]  # 有回复内容

    @pytest.mark.anyio
    async def test_preset_with_params_override(self, mock_chat_completion_rag):
        """preset + params 覆盖：用户指定参数覆盖预设默认值。"""
        with patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_rag)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={
                        "message": "充能50以上的Caster",
                        "mode": "skill",
                        "preset_name": "cycle_farming",
                        "params": {
                            "search_by_np_charge": {"op": "gte", "value": 50},
                            "search_by_class": {"className": "Caster"},
                        },
                    },
                )
        assert resp.status_code == 200
        data = resp.json()
        # 应用了覆盖参数
        skill_calls = data["query"].get("skill_calls", [])
        assert len(skill_calls) > 0

    @pytest.mark.anyio
    async def test_unknown_preset_returns_error(self):
        """未知 preset_name 应返回错误。"""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "test",
                    "mode": "skill",
                    "preset_name": "nonexistent_preset",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "未知的预设名称" in data["reply"]
        assert data["model"] == "error"


# ============================================================
# Skill 模式：直传 params 路径测试
# ============================================================


class TestSkillModeDirectParams:
    """测试 skill 模式下前端直传 params 调用。"""

    @pytest.mark.anyio
    async def test_direct_params_list(self, mock_chat_completion_rag):
        """直传 params 列表格式：[{"skill_name": ..., "params": ...}]。"""
        with patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_rag)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={
                        "message": "五星Saber",
                        "mode": "skill",
                        "params": [
                            {"skill_name": "search_by_class", "params": {"className": "Saber"}},
                            {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
                        ],
                    },
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        # 所有结果应为 Saber 且 5 星
        for s in data["servants"]:
            assert s.get("className", "").lower() == "saber"
            assert s.get("rarity") == 5

    @pytest.mark.anyio
    async def test_direct_params_dict(self, mock_chat_completion_rag):
        """直传 params dict 格式（单个 skill_call）。"""
        with patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_rag)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={
                        "message": "Archer从者",
                        "mode": "skill",
                        "params": {"skill_name": "search_by_class", "params": {"className": "Archer"}},
                    },
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        for s in data["servants"]:
            assert s.get("className", "").lower() == "archer"


# ============================================================
# Agent 路由路径测试（OneShot 已废弃，Agent 为唯一路由入口）
# ============================================================


class TestAgentRouting:
    """测试 Agent 路由模式（唯一路由入口）。"""

    @pytest.mark.anyio
    async def test_agent_routing_success(self):
        """Agent 路由成功：查询 Saber 从者，走 Agent Loop + Generation。"""
        from server.agent.agent_loop import AgentResult

        mock_result = AgentResult(
            reply="找到了一些 Saber 从者",
            rounds=2,
            total_tokens=1000,
            elapsed_ms=500,
            tool_trace=[
                {
                    "round": 1,
                    "tool": "search_servants",
                    "args": {"class_name": "Saber"},
                    "result_summary": "找到 20 位",
                },
            ],
            is_fallback=False,
            servants_data=[
                {"name": "Artoria", "aliasCN": "阿尔托莉雅", "className": "Saber", "rarity": 5},
            ],
        )

        async def mock_gen(**kwargs):
            return {"text": "为你找到了 Saber 职阶的从者，包括阿尔托莉雅等。"}

        with (
            patch("server.main.agent_route", new=AsyncMock(return_value=mock_result)),
            patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_gen)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/chat", json={"message": "有哪些Saber"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        assert data["query"]["mode"] == "agent"
        assert "agent_" in data["model"]

    @pytest.mark.anyio
    async def test_agent_routing_out_of_scope(self):
        """Agent fallback — 超出能力范围的问题应返回 OUT_OF_SCOPE 模板。"""
        from server.agent.agent_loop import AgentResult

        mock_result = AgentResult(
            reply="[OUT_OF_SCOPE] 这个问题与 FGO 无关",
            rounds=1,
            total_tokens=500,
            elapsed_ms=200,
            tool_trace=[],
            is_fallback=True,
            servants_data=[],
        )

        with patch("server.main.agent_route", new=AsyncMock(return_value=mock_result)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/chat", json={"message": "今天天气怎么样"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert "超出了我的能力范围" in data["reply"]

    @pytest.mark.anyio
    async def test_agent_routing_greeting(self):
        """Agent fallback — 问候语应返回 GREETING 模板。"""
        from server.agent.agent_loop import AgentResult

        mock_result = AgentResult(
            reply="[GREETING] 你好",
            rounds=1,
            total_tokens=300,
            elapsed_ms=100,
            tool_trace=[],
            is_fallback=True,
            servants_data=[],
        )

        with patch("server.main.agent_route", new=AsyncMock(return_value=mock_result)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/chat", json={"message": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert "Laplace" in data["reply"]
        assert "FGO" in data["reply"]

    @pytest.mark.anyio
    async def test_agent_routing_error(self):
        """Agent 路由异常：应返回友好错误而非 500。"""

        with patch("server.main.agent_route", new=AsyncMock(side_effect=ConnectionError("Network error"))):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post("/api/chat", json={"message": "查一下Saber"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "error"
        assert "重试" in data["reply"]


# ============================================================
# Preset B1 策略测试：补充文字走 Stage 2 LLM 路由解析额外 Skills
# ============================================================


class TestPresetB1SupplementParsing:
    """测试 preset 模式下补充文字走 Stage 2 解析额外 Skills。"""

    @pytest.mark.anyio
    async def test_supplement_triggers_stage2_routing(self):
        """preset + 非空 message 应触发 Stage 2 LLM 路由解析额外 Skills。"""
        routing_call_count = 0

        async def side_effect(**kwargs):
            nonlocal routing_call_count
            if kwargs.get("json_mode") is True:
                routing_call_count += 1
                # Stage 2 路由：返回额外的 search_by_skill_effect
                return {
                    "skill_calls": [{"skill_name": "search_by_skill_effect", "params": {"effect": "invincible"}}],
                    "response_skill": "respond_servant_list",
                    "fallback": None,
                    "_model": "mock-routing",
                }
            # RAG 生成阶段
            return {"text": "这是 mock 生成的回复。", "_model": "mock-rag"}

        with patch("server.main.chat_completion", new=AsyncMock(side_effect=side_effect)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={
                        "message": "有无敌技能的",
                        "mode": "skill",
                        "preset_name": "cycle_farming",
                    },
                )
        assert resp.status_code == 200
        data = resp.json()
        # Stage 2 路由应被调用一次
        assert routing_call_count == 1
        # 合并后的 skill_calls 应包含预设 Skills + 额外的 search_by_skill_effect
        skill_names = [sc["skill_name"] for sc in data["query"]["skill_calls"]]
        assert "search_by_skill_effect" in skill_names
        # 预设的 search_by_np_charge 也应存在
        assert "search_by_np_charge" in skill_names

    @pytest.mark.anyio
    async def test_empty_message_skips_stage2(self, mock_chat_completion_rag):
        """preset + 空 message 不应触发 Stage 2 路由。"""
        routing_called = False

        original_side_effect = mock_chat_completion_rag

        async def side_effect(**kwargs):
            nonlocal routing_called
            if kwargs.get("json_mode") is True:
                routing_called = True
            return await original_side_effect(**kwargs)

        with patch("server.main.chat_completion", new=AsyncMock(side_effect=side_effect)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={
                        "message": "",
                        "mode": "skill",
                        "preset_name": "cycle_farming",
                    },
                )
        assert resp.status_code == 200
        # 空 message 不应触发 Stage 2 路由
        assert not routing_called
