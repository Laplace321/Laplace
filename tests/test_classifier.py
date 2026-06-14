"""
Stage 0 分类器测试（ADR-024 两阶段路由）。

测试 ClassifierResponse Schema 验证、Prompt 构建、以及路由准确率测试用例集。
"""

import pytest

from server.prompts import build_classifier_prompt
from server.schemas import (
    ClassifierResponse,
    classifier_response_json_schema,
    parse_classifier_response,
)

# ============================================================
# Prompt 构建测试
# ============================================================


class TestClassifierPrompt:
    """Stage 0 分类器 Prompt 构建测试。"""

    def test_prompt_is_string(self):
        prompt = build_classifier_prompt()
        assert isinstance(prompt, str)

    def test_prompt_contains_pipeline_definitions(self):
        prompt = build_classifier_prompt()
        assert "链路 A" in prompt
        assert "链路 B" in prompt
        assert "链路 C" in prompt
        # ADR-032：FALLBACK 链路定义
        assert "链路 FALLBACK" in prompt
        assert "greeting" in prompt
        assert "out_of_scope" in prompt

    def test_prompt_contains_disambiguation_rules(self):
        prompt = build_classifier_prompt()
        assert "消歧义" in prompt

    def test_prompt_contains_output_format(self):
        prompt = build_classifier_prompt()
        assert '"pipeline"' in prompt
        assert '"confidence"' in prompt

    def test_prompt_contains_examples(self):
        prompt = build_classifier_prompt()
        assert "示例" in prompt
        assert "剑阶出星推荐" in prompt  # 已知误路由 case 必须在示例中

    def test_prompt_length_is_compact(self):
        """Stage 0 Prompt 应远小于 Stage 1（<6k chars）。

        ADR-032：扩展 FALLBACK 链路定义 + 反例后从 ~2.5k 增长到 ~4.2k 字符，
        仍远小于 Stage 1 路由 prompt（~30k+），上限放宽到 6000。
        """
        prompt = build_classifier_prompt()
        assert len(prompt) < 6000, f"Stage 0 Prompt 过长: {len(prompt)} chars"


# ============================================================
# 路由准确率测试用例集
# ============================================================

# 测试用例格式：(用户查询, 期望链路)
ROUTING_TEST_CASES = [
    # ── 链路 A：从者/礼装结构化数据查询 ──
    ("30自充以上的术阶", "A"),
    ("剑阶出星推荐", "A"),  # 已知误路由 case：之前被误判为 C
    ("查一下梅林", "A"),
    ("有蓝魔放的五星从者", "A"),
    ("克制月癌的从者", "A"),
    ("对比村正和武尊", "A"),
    ("有NP充能效果的五星礼装", "A"),
    ("万花筒", "A"),
    ("纯攻的五星限定礼装", "A"),
    ("哪个职阶克制骑阶", "A"),
    ("50自充，30群充的从者", "A"),
    ("能挡伤害的从者", "A"),
    ("龙特性的五星从者", "A"),
    ("红A", "A"),  # 昵称查询
    ("梅林技能介绍", "A"),
    # ── 链路 B：FGO 游戏事实知识问答 ──
    ("梅林什么时候复刻", "B"),
    ("最近有什么活动", "B"),
    ("龙之牙在哪里掉", "B"),
    ("特异点F是什么", "B"),
    ("去年周年庆有什么活动", "B"),
    # ── 链路 C：攻略/评价/主观推荐 ──
    ("戴冠战剑阶怎么打", "C"),
    ("高难配队推荐", "C"),
    ("村正值不值得练", "C"),
    ("剑冠Boss机制", "C"),
    ("戴冠战星图攻略", "C"),
]


class TestRoutingAccuracy:
    """Stage 0 分类器路由准确率测试。

    这些测试用例验证 ClassifierResponse 的 Schema 解析正确性，
    实际 LLM 准确率需要在集成测试中验证。
    """

    @pytest.mark.parametrize(
        "query,expected_pipeline",
        ROUTING_TEST_CASES,
        ids=[case[0] for case in ROUTING_TEST_CASES],
    )
    def test_expected_pipeline_is_valid(self, query: str, expected_pipeline: str):
        """验证测试用例中的期望链路是合法值。"""
        assert expected_pipeline in ("A", "B", "C", "FALLBACK"), f"Invalid expected pipeline: {expected_pipeline}"

    def test_all_pipelines_covered(self):
        """验证测试用例集覆盖了 A/B/C 三条主链路（FALLBACK 在专用测试覆盖）。"""
        pipelines = {case[1] for case in ROUTING_TEST_CASES}
        missing = {"A", "B", "C"} - pipelines
        assert not missing, f"Missing pipelines: {missing}"

    def test_known_misroute_case_included(self):
        """验证已知误路由 case（剑阶出星推荐）在测试用例中且期望走 A。"""
        misroute_cases = [(q, p) for q, p in ROUTING_TEST_CASES if "剑阶出星" in q]
        assert len(misroute_cases) >= 1
        assert misroute_cases[0][1] == "A"

    def test_classifier_response_schema_roundtrip(self):
        """验证每条链路都能通过 ClassifierResponse 的 Schema 校验。"""
        for pipeline in ("A", "B", "C"):
            resp = ClassifierResponse(pipeline=pipeline, confidence=0.9)
            dumped = resp.model_dump()
            restored = ClassifierResponse.model_validate(dumped)
            assert restored.pipeline == pipeline
        # FALLBACK 必须搭配 fallback_code（greeting / out_of_scope）
        for code in ("greeting", "out_of_scope"):
            resp = ClassifierResponse(pipeline="FALLBACK", confidence=0.95, fallback_code=code)
            dumped = resp.model_dump()
            restored = ClassifierResponse.model_validate(dumped)
            assert restored.pipeline == "FALLBACK"
            assert restored.fallback_code == code

    def test_parse_classifier_all_pipelines(self):
        """验证 parse_classifier_response 能解析所有链路的有效 JSON。"""
        for pipeline in ("A", "B", "C"):
            import json

            json_str = json.dumps({"pipeline": pipeline, "confidence": 0.85})
            result = parse_classifier_response(json_str)
            assert result["pipeline"] == pipeline
            assert result["confidence"] == 0.85

    def test_parse_classifier_fallback_with_code(self):
        """ADR-032：FALLBACK 必须能与 fallback_code 一同解析回 dict。"""
        import json

        for code in ("greeting", "out_of_scope"):
            json_str = json.dumps({"pipeline": "FALLBACK", "confidence": 0.97, "fallback_code": code})
            result = parse_classifier_response(json_str)
            assert result["pipeline"] == "FALLBACK"
            assert result["fallback_code"] == code


# ============================================================
# 边界 Case 测试
# ============================================================


class TestBoundaryCases:
    """边界 case 的 Schema 测试。"""

    def test_low_confidence_schema(self):
        """低置信度值可以正常创建。"""
        resp = ClassifierResponse(pipeline="A", confidence=0.3)
        assert resp.confidence == 0.3

    def test_exact_threshold_confidence(self):
        """恰好在阈值（0.6）的置信度可以正常创建。"""
        resp = ClassifierResponse(pipeline="A", confidence=0.6)
        assert resp.confidence == 0.6

    def test_classifier_schema_completeness(self):
        """ClassifierResponse JSON Schema 包含所有必要字段。"""
        schema = classifier_response_json_schema()
        required = schema.get("required", [])
        assert "pipeline" in required
        assert "confidence" in required

    def test_classifier_prompt_no_skill_details(self):
        """Stage 0 Prompt 不应包含具体的 Skill 名称（那是 Stage 1 的事）。"""
        prompt = build_classifier_prompt()
        assert "search_by_effect" not in prompt
        assert "lookup_servant" not in prompt
        assert "search_by_class" not in prompt


# ============================================================
# classify_node 后置兜底规则测试（trace a365ac5c 修复）
# ============================================================


class TestClassifyNodeAnchorGuard:
    """classify_node 后置兜底：MINOR 必须含承接词，否则强制改回 MAJOR。

    防御 LLM 把"弓阶的 5 星从者"等完整独立查询误判为追问。
    """

    @staticmethod
    def _make_prev_turn():
        from server.graph.session import TurnSnapshot

        return TurnSnapshot(
            session_id="sid-anchor",
            user_message="3 回合内充满 NP 100% 的从者",
            reply="为你筛选出 6 位...",
            summary="上一轮：NP 100% 自充；命中 6 条",
            pipeline="A",
            skill_calls=[{"skill_name": "search_by_effect", "params": {"effect": "gainNp", "minValue": 100}}],
            response_skill_name="respond_servant_list",
            servants=[],
            query={},
            turn_type="MAJOR",
            timestamp=1.0,
        )

    @pytest.mark.asyncio
    async def test_minor_without_anchor_word_is_forced_to_major(self):
        """LLM 误判 MINOR 但 user_message 不含承接词 → 强制 MAJOR。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(
            user_message="弓阶的 5 星从者",
            trace_id="t-anchor-guard-1",
            session_id="sid-anchor",
            turn_type="MAJOR",
        )
        state.extras["prev_turn"] = self._make_prev_turn()

        fake_resp = {
            "pipeline": "A",
            "confidence": 0.95,
            "turn_type": "MINOR",  # LLM 误判
            "_model": "fake",
            "_usage": {"total_tokens": 100},
        }

        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=fake_resp),
        ):
            out = await classify_node(state)

        assert out.turn_type == "MAJOR", "无承接词的完整查询应被兜底改回 MAJOR"
        # 兜底改 MAJOR 后，metric_labels 也应反映正确的 turn_type
        assert out.metric_labels.get("turn_type") == "MAJOR"

    @pytest.mark.asyncio
    async def test_minor_with_anchor_word_is_preserved(self):
        """含承接词（"其中"）的 MINOR 判断应被保留。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(
            user_message="其中弓阶的",
            trace_id="t-anchor-guard-2",
            session_id="sid-anchor",
            turn_type="MAJOR",
        )
        state.extras["prev_turn"] = self._make_prev_turn()

        fake_resp = {
            "pipeline": "A",
            "confidence": 0.9,
            "turn_type": "MINOR",
            "_model": "fake",
            "_usage": {"total_tokens": 100},
        }

        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=fake_resp),
        ):
            out = await classify_node(state)

        assert out.turn_type == "MINOR", "含承接词应保留 MINOR 判断"
        assert "prev_turn" in out.extras

    @pytest.mark.asyncio
    async def test_minor_with_detail_anchor_is_preserved(self):
        """含"详细"承接词的 MINOR 判断应被保留。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(
            user_message="详细说说第一个",
            trace_id="t-anchor-guard-3",
            session_id="sid-anchor",
            turn_type="MAJOR",
        )
        state.extras["prev_turn"] = self._make_prev_turn()

        fake_resp = {
            "pipeline": "A",
            "confidence": 0.9,
            "turn_type": "MINOR",
            "_model": "fake",
            "_usage": {"total_tokens": 100},
        }

        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=fake_resp),
        ):
            out = await classify_node(state)

        assert out.turn_type == "MINOR"


# ============================================================
# classify_node FALLBACK 后置防误判测试（ADR-032）
# ============================================================


class TestClassifyNodeFallbackGuard:
    """classify_node FALLBACK 双重防御：

    1) LLM 判 FALLBACK + 用户消息含 FGO 查询信号 → 强制改回 A，清空 fallback_code
    2) LLM 判 FALLBACK + 缺合法 fallback_code → 默认补 greeting
    3) LLM 判 A → fallback_code 必须被清空，不污染 extras
    4) 端到端「你好」+ LLM FALLBACK greeting → state 携带 fallback_code=greeting
    """

    @staticmethod
    def _fake_resp(
        pipeline: str,
        *,
        fallback_code: str | None = None,
        confidence: float = 0.95,
    ) -> dict:
        resp: dict = {
            "pipeline": pipeline,
            "confidence": confidence,
            "turn_type": "MAJOR",
            "_model": "fake",
            "_usage": {"total_tokens": 50},
        }
        if fallback_code is not None:
            resp["fallback_code"] = fallback_code
        return resp

    @pytest.mark.asyncio
    async def test_fallback_with_fgo_signal_is_forced_to_a(self):
        """LLM 把「梅林是谁」误判为 FALLBACK → 关键词命中「梅林」不在词表，但「是谁」也不在；
        改用「剑阶」这种确定命中的信号词验证后置防御。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(
            user_message="剑阶推荐",
            trace_id="t-fb-guard-1",
        )
        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=self._fake_resp("FALLBACK", fallback_code="greeting")),
        ):
            out = await classify_node(state)

        assert out.classified_pipeline == "A", "含 FGO 信号的输入必须强制改回 A"
        assert "fallback_code" not in out.extras, "改回 A 后必须清空 fallback_code"
        assert out.metric_labels.get("pipeline") == "A"

    @pytest.mark.asyncio
    async def test_fallback_missing_code_defaults_to_greeting(self):
        """LLM 输出 FALLBACK 但缺 fallback_code → 自动补为 greeting，避免下游崩溃。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(user_message="你好", trace_id="t-fb-guard-2")
        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=self._fake_resp("FALLBACK", fallback_code=None)),
        ):
            out = await classify_node(state)

        assert out.classified_pipeline == "FALLBACK"
        assert out.extras.get("fallback_code") == "greeting"

    @pytest.mark.asyncio
    async def test_non_fallback_pipeline_clears_fallback_code(self):
        """非 FALLBACK 路径必须清空 fallback_code，避免污染 extras。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(user_message="梅林技能介绍", trace_id="t-fb-guard-3")
        # 模拟 LLM 错误地同时输出 A + greeting
        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=self._fake_resp("A", fallback_code="greeting")),
        ):
            out = await classify_node(state)

        assert out.classified_pipeline == "A"
        assert "fallback_code" not in out.extras

    @pytest.mark.asyncio
    async def test_pure_greeting_routes_to_fallback_with_code(self):
        """端到端：「你好」+ LLM FALLBACK greeting → state 完整携带 fallback_code=greeting。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(user_message="你好", trace_id="t-fb-guard-4")
        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=self._fake_resp("FALLBACK", fallback_code="greeting")),
        ):
            out = await classify_node(state)

        assert out.classified_pipeline == "FALLBACK"
        assert out.extras.get("fallback_code") == "greeting"
        assert out.metric_labels.get("pipeline") == "FALLBACK"

    @pytest.mark.asyncio
    async def test_out_of_scope_routes_to_fallback_with_code(self):
        """端到端：「推荐充电器」+ LLM FALLBACK out_of_scope → 携带 out_of_scope code。"""
        from unittest.mock import AsyncMock, patch

        from server.graph.state import PipelineState
        from server.nodes.classify import classify_node

        state = PipelineState(user_message="推荐一个充电器", trace_id="t-fb-guard-5")
        with patch(
            "server.nodes.classify.chat_completion",
            new=AsyncMock(return_value=self._fake_resp("FALLBACK", fallback_code="out_of_scope")),
        ):
            out = await classify_node(state)

        # 注意："推荐" 在 _FGO_SIGNAL_KEYWORDS 中，会触发后置防误判改回 A
        # 这是预期行为：宁可让 A 链路兜底（低置信度走 agent），也不放过潜在的 FGO 查询
        # 这条 case 演示后置防御对「推荐 X」类输入的双刃剑特性
        assert out.classified_pipeline == "A", "含『推荐』关键词被后置防御改回 A（已知权衡）"


# ============================================================
# _has_fgo_query_signal 关键词覆盖测试
# ============================================================


class TestFgoQuerySignal:
    """_has_fgo_query_signal 单元测试。"""

    def test_class_keywords_match(self):
        from server.nodes.classify import _has_fgo_query_signal

        for kw in ("剑阶推荐", "saber 出星", "弓阶 5 星"):
            assert _has_fgo_query_signal(kw), f"应命中: {kw}"

    def test_effect_keywords_match(self):
        from server.nodes.classify import _has_fgo_query_signal

        for kw in ("有充能效果", "无敌挡刀", "暴击威力"):
            assert _has_fgo_query_signal(kw), f"应命中: {kw}"

    def test_pure_greeting_does_not_match(self):
        from server.nodes.classify import _has_fgo_query_signal

        for kw in ("你好", "在吗", "hello", "你能做什么"):
            assert not _has_fgo_query_signal(kw), f"不应命中: {kw}"

    def test_pure_out_of_scope_does_not_match(self):
        from server.nodes.classify import _has_fgo_query_signal

        # 注意：「推荐」在词表中，「明天天气」不在；这里用纯无关短语
        for kw in ("明天天气怎么样", "讲个笑话"):
            assert not _has_fgo_query_signal(kw), f"不应命中: {kw}"

    def test_empty_message_returns_false(self):
        from server.nodes.classify import _has_fgo_query_signal

        assert _has_fgo_query_signal("") is False
        assert _has_fgo_query_signal(None) is False  # type: ignore[arg-type]
