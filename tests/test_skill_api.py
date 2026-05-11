"""
Skill 模式 API 集成测试。

测试范围：
- chat() 的 mode 分发（natural_language vs skill）
- skill 模式下 preset / params / B1 补充解析各路径
- _handle_skill_mode() 的 LLM 路由路径（mock chat_completion）
- 未知 preset 错误处理
- 旧模式（natural_language）不受影响

使用 FastAPI TestClient + mock chat_completion，SkillExecutor 真实执行。
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


@pytest.fixture
def mock_chat_completion_routing_and_rag():
    """Mock chat_completion 同时覆盖路由阶段和 RAG 阶段。"""

    async def side_effect(**kwargs):
        if kwargs.get("json_mode") is True:
            # 路由阶段：返回 search_by_class Saber
            return {
                "skill_calls": [{"skill_name": "search_by_class", "params": {"className": "Saber"}}],
                "response_skill": "respond_servant_list",
                "fallback": None,
                "_model": "mock-routing",
            }
        else:
            # RAG 生成阶段
            return {"text": "为你找到了多位 Saber 职阶从者。", "_model": "mock-rag"}

    return side_effect


@pytest.fixture
def mock_chat_completion_fallback():
    """Mock chat_completion 返回 fallback（路由无法匹配）。"""

    async def side_effect(**kwargs):
        if kwargs.get("json_mode") is True:
            return {
                "skill_calls": [],
                "response_skill": "respond_servant_list",
                "fallback": {"code": "out_of_scope", "message": "这不是 FGO 相关的问题。"},
                "_model": "mock-routing",
            }
        return {"text": "", "_model": "mock-rag"}

    return side_effect


@pytest.fixture
def mock_chat_completion_empty_skills():
    """Mock chat_completion 返回空 skill_calls 且无 fallback。"""

    async def side_effect(**kwargs):
        if kwargs.get("json_mode") is True:
            return {
                "skill_calls": [],
                "response_skill": "respond_servant_list",
                "fallback": None,
                "_model": "mock-routing",
            }
        return {"text": "", "_model": "mock-rag"}

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
        assert data["query"]["mode"] == "skill"
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
# Skill 模式：LLM 路由路径测试
# ============================================================


class TestSkillModeLLMRouting:
    """测试 skill 模式下走 LLM 路由。"""

    @pytest.mark.anyio
    async def test_llm_routing_success(self, mock_chat_completion_routing_and_rag):
        """LLM 路由成功：返回 Saber 职阶从者。"""
        with patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_routing_and_rag)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={"message": "有哪些Saber", "mode": "skill"},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        assert "Saber" in data["reply"] or "从者" in data["reply"]
        assert data["model"] == "mock-routing"

    @pytest.mark.anyio
    async def test_llm_routing_fallback(self, mock_chat_completion_fallback):
        """LLM 路由 fallback（out_of_scope）：返回模板回复。"""
        with patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_fallback)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={"message": "今天天气怎么样", "mode": "skill"},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        # out_of_scope 走模板回复
        assert "超出" in data["reply"] or "能力范围" in data["reply"]

    @pytest.mark.anyio
    async def test_llm_routing_empty_skills_no_fallback(self, mock_chat_completion_empty_skills):
        """LLM 返回空 skill_calls 且无 fallback — 触发 Agent 兜底。"""
        from server.agent.agent_loop import AgentResult

        mock_agent_result = AgentResult(
            reply="[OUT_OF_SCOPE] 无法处理",
            rounds=1,
            total_tokens=100,
            servants_data=[],
        )
        with (
            patch("server.main.chat_completion", new=AsyncMock(side_effect=mock_chat_completion_empty_skills)),
            patch("server.main.agent_route", new=AsyncMock(return_value=mock_agent_result)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={"message": "嗯嗯", "mode": "skill"},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        # Agent 返回 OUT_OF_SCOPE → 走模板回复
        assert "超出" in data["reply"] or "能力范围" in data["reply"]

    @pytest.mark.anyio
    async def test_llm_routing_error(self):
        """LLM 路由异常：应返回友好错误而非 500。"""

        async def raise_error(**kwargs):
            raise ConnectionError("Network error")

        with patch("server.main.chat_completion", new=AsyncMock(side_effect=raise_error)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/chat",
                    json={"message": "查一下Saber", "mode": "skill"},
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "error"
        assert "路由" in data["reply"] or "重试" in data["reply"]


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
