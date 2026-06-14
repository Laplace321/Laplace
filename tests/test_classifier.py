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
        """Stage 0 Prompt 应远小于 Stage 1（<3k chars）。"""
        prompt = build_classifier_prompt()
        assert len(prompt) < 3000, f"Stage 0 Prompt 过长: {len(prompt)} chars"


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
        assert expected_pipeline in ("A", "B", "C"), f"Invalid expected pipeline: {expected_pipeline}"

    def test_all_pipelines_covered(self):
        """验证测试用例集覆盖了所有三条链路。"""
        pipelines = {case[1] for case in ROUTING_TEST_CASES}
        missing = {"A", "B", "C"} - pipelines
        assert pipelines == {"A", "B", "C"}, f"Missing pipelines: {missing}"

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

    def test_parse_classifier_all_pipelines(self):
        """验证 parse_classifier_response 能解析所有链路的有效 JSON。"""
        for pipeline in ("A", "B", "C"):
            import json

            json_str = json.dumps({"pipeline": pipeline, "confidence": 0.85})
            result = parse_classifier_response(json_str)
            assert result["pipeline"] == pipeline
            assert result["confidence"] == 0.85


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
