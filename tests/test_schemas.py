"""
Schema 单元测试。

测试 RoutingResponse / SkillCall / FallbackReason 的序列化/反序列化。
"""

import json

import pytest

from server.schemas import (
    ClassifierResponse,
    FallbackReason,
    RoutingResponse,
    SkillCall,
    classifier_response_json_schema,
    parse_classifier_response,
    parse_routing_response,
    routing_response_json_schema,
)

# ============================================================
# SkillCall 测试
# ============================================================


class TestSkillCall:
    def test_basic_creation(self):
        call = SkillCall(skill_name="search_by_class", params={"className": "Caster"})
        assert call.skill_name == "search_by_class"
        assert call.params == {"className": "Caster"}

    def test_empty_params_default(self):
        call = SkillCall(skill_name="lookup_servant")
        assert call.params == {}

    def test_extra_fields_ignored(self):
        call = SkillCall(skill_name="test", params={}, extra_field="should_be_ignored")
        assert call.skill_name == "test"


# ============================================================
# FallbackReason 测试
# ============================================================


class TestFallbackReason:
    def test_default_code(self):
        fb = FallbackReason()
        assert fb.code == "no_match"
        assert fb.message == ""

    def test_custom_code(self):
        fb = FallbackReason(code="out_of_scope", message="不支持此类查询")
        assert fb.code == "out_of_scope"


# ============================================================
# RoutingResponse 测试
# ============================================================


class TestRoutingResponse:
    def test_basic_routing(self):
        resp = RoutingResponse(
            skill_calls=[SkillCall(skill_name="search_by_class", params={"className": "Saber"})],
            response_skill="respond_servant_list",
        )
        assert len(resp.skill_calls) == 1
        assert resp.response_skill == "respond_servant_list"
        assert resp.fallback is None

    def test_multi_skill_routing(self):
        resp = RoutingResponse(
            skill_calls=[
                SkillCall(skill_name="search_by_class", params={"className": "Caster"}),
                SkillCall(skill_name="search_by_rarity", params={"op": "eq", "value": 5}),
            ],
            response_skill="respond_servant_list",
        )
        assert len(resp.skill_calls) == 2

    def test_fallback_routing(self):
        resp = RoutingResponse(
            skill_calls=[],
            fallback=FallbackReason(code="no_match", message="无法理解"),
        )
        assert resp.fallback is not None
        assert resp.fallback.code == "no_match"

    def test_serialization_roundtrip(self):
        original = RoutingResponse(
            skill_calls=[SkillCall(skill_name="lookup_servant", params={"name": "梅林"})],
            response_skill="respond_servant_detail",
        )
        dumped = original.model_dump(exclude_none=True)
        restored = RoutingResponse.model_validate(dumped)
        assert restored.skill_calls[0].skill_name == "lookup_servant"
        assert restored.response_skill == "respond_servant_detail"

    def test_from_json_string(self):
        json_str = '{"skill_calls": [{"skill_name": "search_by_effect", "params": {"effect": "gainNp", "targetType": "self", "minValue": 50}}], "response_skill": "respond_servant_list"}'
        data = json.loads(json_str)
        resp = RoutingResponse.model_validate(data)
        assert resp.skill_calls[0].params["minValue"] == 50


# ============================================================
# JSON Schema 生成测试
# ============================================================


class TestJsonSchemas:
    def test_routing_schema_has_required_fields(self):
        schema = routing_response_json_schema()
        props = schema.get("properties", {})
        assert "skill_calls" in props
        assert "response_skill" in props


# ============================================================
# parse_routing_response 测试
# ============================================================


class TestParseRoutingResponse:
    def test_parse_valid_json(self):
        json_str = '{"skill_calls": [{"skill_name": "search_by_class", "params": {"className": "Archer"}}], "response_skill": "respond_servant_list"}'
        result = parse_routing_response(json_str)
        assert len(result["skill_calls"]) == 1
        assert result["skill_calls"][0]["skill_name"] == "search_by_class"

    def test_parse_dict_input(self):
        data = {
            "skill_calls": [{"skill_name": "lookup_servant", "params": {"name": "Merlin"}}],
            "response_skill": "respond_servant_detail",
        }
        result = parse_routing_response(data)
        assert result["response_skill"] == "respond_servant_detail"

    def test_parse_with_fenced_json(self):
        content = '```json\n{"skill_calls": [], "response_skill": "respond_servant_list", "fallback": {"code": "no_match", "message": "test"}}\n```'
        result = parse_routing_response(content)
        assert result["fallback"]["code"] == "no_match"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(ValueError):
            parse_routing_response("not json at all")

    def test_parse_invalid_schema_raises(self):
        with pytest.raises(ValueError):
            parse_routing_response('{"skill_calls": "not_a_list"}')


# ============================================================
# RoutingResponse 独立验证测试
# ============================================================


class TestRoutingResponseValidation:
    def test_routing_response_validates(self):
        """RoutingResponse 可以正常使用。"""
        data = {
            "skill_calls": [{"skill_name": "search_by_class", "params": {"className": "Saber"}}],
            "response_skill": "respond_servant_list",
        }
        resp = RoutingResponse.model_validate(data)
        assert len(resp.skill_calls) == 1

    def test_unrelated_data_has_empty_skill_calls(self):
        """无关数据解析为 RoutingResponse 时 skill_calls 为空。"""
        data = {"intent": "query_servants", "conditions": {"className": "Saber"}}
        resp = RoutingResponse.model_validate(data)
        assert resp.skill_calls == []


# ============================================================
# Clarification 机制测试
# ============================================================


class TestClarification:
    """用户确认机制（clarification）相关测试。"""

    def test_clarification_option_creation(self):
        from server.schemas import ClarificationOption

        opt = ClarificationOption(id="party", label="全队暴击伤害UP")
        assert opt.id == "party"
        assert opt.label == "全队暴击伤害UP"

    def test_clarification_request_creation(self):
        from server.schemas import ClarificationOption, ClarificationRequest

        req = ClarificationRequest(
            question="你想查哪种类型的暴击拐？",
            options=[
                ClarificationOption(id="party", label="全队暴击伤害UP"),
                ClarificationOption(id="self", label="自身暴击伤害UP"),
            ],
            ambiguous_field="targetType",
        )
        assert req.question == "你想查哪种类型的暴击拐？"
        assert len(req.options) == 2
        assert req.ambiguous_field == "targetType"

    def test_clarification_request_default_ambiguous_field(self):
        from server.schemas import ClarificationOption, ClarificationRequest

        req = ClarificationRequest(
            question="确认问题",
            options=[ClarificationOption(id="a", label="选项A")],
        )
        assert req.ambiguous_field == ""

    def test_routing_response_with_clarification(self):
        """RoutingResponse 含 clarification 时 skill_calls 应为空。"""
        data = {
            "skill_calls": [],
            "response_skill": "respond_servant_list",
            "clarification": {
                "question": "你想查哪种类型的暴击拐？",
                "options": [
                    {"id": "party", "label": "全队暴击伤害UP"},
                    {"id": "self", "label": "自身暴击伤害UP"},
                    {"id": "ptOne", "label": "给单个队友暴击伤害UP"},
                ],
                "ambiguous_field": "targetType",
            },
        }
        resp = RoutingResponse.model_validate(data)
        assert resp.skill_calls == []
        assert resp.clarification is not None
        assert resp.clarification.question == "你想查哪种类型的暴击拐？"
        assert len(resp.clarification.options) == 3
        assert resp.clarification.options[0].id == "party"

    def test_routing_response_without_clarification(self):
        """无 clarification 时字段为 None，原有流程不受影响。"""
        data = {
            "skill_calls": [{"skill_name": "search_by_effect", "params": {"effect": "gainNp"}}],
            "response_skill": "respond_servant_list",
        }
        resp = RoutingResponse.model_validate(data)
        assert resp.clarification is None
        assert len(resp.skill_calls) == 1

    def test_parse_routing_response_with_clarification(self):
        """parse_routing_response 正确解析含 clarification 的 JSON。"""
        json_str = json.dumps(
            {
                "skill_calls": [],
                "response_skill": "respond_servant_list",
                "fallback": None,
                "target_pipeline": None,
                "clarification": {
                    "question": "你想查哪种暴击拐？",
                    "options": [{"id": "party", "label": "全队暴击UP"}],
                    "ambiguous_field": "targetType",
                },
            }
        )
        result = parse_routing_response(json_str)
        assert result["clarification"] is not None
        assert result["clarification"]["question"] == "你想查哪种暴击拐？"
        assert result["skill_calls"] == []

    def test_parse_routing_response_without_clarification_unchanged(self):
        """无 clarification 时 parse_routing_response 行为与之前完全一致。"""
        json_str = '{"skill_calls": [{"skill_name": "search_by_class", "params": {"className": "Archer"}}], "response_skill": "respond_servant_list"}'
        result = parse_routing_response(json_str)
        assert result.get("clarification") is None
        assert len(result["skill_calls"]) == 1

    def test_json_schema_includes_clarification(self):
        """routing_response_json_schema 输出中包含 clarification 字段定义。"""
        schema = routing_response_json_schema()
        props = schema.get("properties", {})
        assert "clarification" in props

    def test_clarification_extra_fields_ignored(self):
        """ClarificationOption/Request 的 extra 字段被忽略。"""
        from server.schemas import ClarificationOption, ClarificationRequest

        opt = ClarificationOption(id="x", label="test", unknown_field="ignored")
        assert opt.id == "x"

        req = ClarificationRequest(
            question="q",
            options=[ClarificationOption(id="a", label="A")],
            extra="ignored",
        )
        assert req.question == "q"


# ============================================================
# ClassifierResponse 测试（Stage 0 分类器, ADR-024）
# ============================================================


class TestClassifierResponse:
    """Stage 0 分类器 Schema 测试。"""

    def test_basic_creation(self):
        resp = ClassifierResponse(pipeline="A", confidence=0.95)
        assert resp.pipeline == "A"
        assert resp.confidence == 0.95

    def test_all_pipelines(self):
        for pipeline in ("A", "B", "C"):
            resp = ClassifierResponse(pipeline=pipeline, confidence=0.8)
            assert resp.pipeline == pipeline

    def test_confidence_boundary_values(self):
        resp_zero = ClassifierResponse(pipeline="A", confidence=0.0)
        assert resp_zero.confidence == 0.0
        resp_one = ClassifierResponse(pipeline="B", confidence=1.0)
        assert resp_one.confidence == 1.0

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(Exception):
            ClassifierResponse(pipeline="A", confidence=1.5)
        with pytest.raises(Exception):
            ClassifierResponse(pipeline="A", confidence=-0.1)

    def test_invalid_pipeline_raises(self):
        with pytest.raises(Exception):
            ClassifierResponse(pipeline="D", confidence=0.5)

    def test_extra_fields_ignored(self):
        resp = ClassifierResponse(pipeline="C", confidence=0.7, extra_field="ignored")
        assert resp.pipeline == "C"

    def test_serialization_roundtrip(self):
        original = ClassifierResponse(pipeline="B", confidence=0.85)
        dumped = original.model_dump()
        restored = ClassifierResponse.model_validate(dumped)
        assert restored.pipeline == "B"
        assert restored.confidence == 0.85

    def test_from_json_string(self):
        import json

        data = json.loads('{"pipeline": "A", "confidence": 0.92}')
        resp = ClassifierResponse.model_validate(data)
        assert resp.pipeline == "A"
        assert resp.confidence == 0.92


class TestClassifierJsonSchema:
    """Stage 0 分类器 JSON Schema 测试。"""

    def test_schema_has_required_fields(self):
        schema = classifier_response_json_schema()
        props = schema.get("properties", {})
        assert "pipeline" in props
        assert "confidence" in props

    def test_pipeline_enum_values(self):
        schema = classifier_response_json_schema()
        pipeline_prop = schema["properties"]["pipeline"]
        assert set(pipeline_prop.get("enum", [])) == {"A", "B", "C"}


class TestParseClassifierResponse:
    """Stage 0 分类器解析函数测试。"""

    def test_parse_valid_json_string(self):
        result = parse_classifier_response('{"pipeline": "A", "confidence": 0.95}')
        assert result["pipeline"] == "A"
        assert result["confidence"] == 0.95

    def test_parse_dict_input(self):
        result = parse_classifier_response({"pipeline": "B", "confidence": 0.8})
        assert result["pipeline"] == "B"

    def test_parse_with_fenced_json(self):
        content = '```json\n{"pipeline": "C", "confidence": 0.7}\n```'
        result = parse_classifier_response(content)
        assert result["pipeline"] == "C"

    def test_parse_invalid_json_raises(self):
        with pytest.raises(ValueError):
            parse_classifier_response("not json")

    def test_parse_invalid_schema_raises(self):
        with pytest.raises(ValueError):
            parse_classifier_response('{"pipeline": "X", "confidence": 0.5}')

    def test_parse_missing_field_raises(self):
        with pytest.raises(ValueError):
            parse_classifier_response('{"pipeline": "A"}')


# ============================================================
# 执行层 Clarification 测试
# ============================================================


class TestExecutionClarification:
    """执行层 clarification（多候选/空结果引导）相关测试。"""

    def test_execution_result_with_clarification(self):
        """ExecutionResult 可携带 clarification 字段。"""
        from server.skills.executor import (
            CLARIFICATION_MULTI_CANDIDATE,
            ExecutionResult,
        )

        clarification = {
            "type": CLARIFICATION_MULTI_CANDIDATE,
            "question": "「伊吹」匹配到多个结果，请选择你要查询的：",
            "options": [
                {"id": "268", "label": "★★★★★ 伊吹童子（Saber）"},
                {"id": "316", "label": "★★★★★ 水着伊吹童子（Berserker）"},
            ],
            "ambiguous_field": "name",
        }
        result = ExecutionResult(
            servants=[{"id": 1}, {"id": 2}],
            total_found=2,
            response_skill=None,
            accepted_skills=[{"skill_name": "lookup_servant", "params": {"name": "伊吹"}}],
            rejected_skills=[],
            execution_time_ms=10.5,
            clarification=clarification,
        )
        assert result.clarification is not None
        assert result.clarification["type"] == CLARIFICATION_MULTI_CANDIDATE
        assert len(result.clarification["options"]) == 2

    def test_execution_result_without_clarification(self):
        """默认 clarification 为 None，不影响现有流程。"""
        from server.skills.executor import ExecutionResult

        result = ExecutionResult(
            servants=[{"id": 1}],
            total_found=1,
            response_skill=None,
            accepted_skills=[],
            rejected_skills=[],
            execution_time_ms=5.0,
        )
        assert result.clarification is None

    def test_clarification_type_constants(self):
        """验证 clarification 类型常量已正确定义。"""
        from server.skills.executor import (
            CLARIFICATION_EMPTY_FILTER,
            CLARIFICATION_EMPTY_NAME,
            CLARIFICATION_MULTI_CANDIDATE,
        )

        assert CLARIFICATION_MULTI_CANDIDATE == "multi_candidate"
        assert CLARIFICATION_EMPTY_NAME == "empty_result_name"
        assert CLARIFICATION_EMPTY_FILTER == "empty_result_filter"

    def test_is_single_name_lookup_servant(self):
        """_is_single_name_lookup 正确判断 servant 单名称查询。"""
        from server.skills.executor import SkillExecutor

        executor = SkillExecutor()
        assert executor._is_single_name_lookup(
            [{"skill_name": "lookup_servant", "params": {"name": "梅林"}}],
            domain="servant",
        )
        assert not executor._is_single_name_lookup(
            [
                {"skill_name": "lookup_servant", "params": {"name": "梅林"}},
                {"skill_name": "search_by_rarity", "params": {"value": 5}},
            ],
            domain="servant",
        )

    def test_is_single_name_lookup_ce(self):
        """_is_single_name_lookup 正确判断 CE 单名称查询。"""
        from server.skills.executor import SkillExecutor

        executor = SkillExecutor()
        assert executor._is_single_name_lookup(
            [{"skill_name": "ce_lookup", "params": {"name": "黑杯"}}],
            domain="ce",
        )
        assert not executor._is_single_name_lookup(
            [{"skill_name": "lookup_servant", "params": {"name": "梅林"}}],
            domain="ce",
        )

    def test_build_multi_candidate_clarification(self):
        """_build_multi_candidate_clarification 正确构建多候选选项。"""
        from server.skills.executor import SkillExecutor

        executor = SkillExecutor()
        results = [
            {"collectionNo": 268, "aliasCN": "伊吹童子", "rarity": 5, "className": "saber"},
            {"collectionNo": 316, "aliasCN": "伊吹童子〔夏〕", "rarity": 5, "className": "berserker"},
        ]
        accepted = [{"skill_name": "lookup_servant", "params": {"name": "伊吹"}}]
        clarification = executor._build_multi_candidate_clarification(results, accepted, domain="servant")
        assert clarification is not None
        assert clarification["type"] == "multi_candidate"
        assert len(clarification["options"]) == 2
        assert "伊吹童子" in clarification["options"][0]["label"]
        assert "268" == clarification["options"][0]["id"]

    def test_build_multi_candidate_returns_none_for_single(self):
        """单个结果时不触发 clarification。"""
        from server.skills.executor import SkillExecutor

        executor = SkillExecutor()
        results = [{"collectionNo": 150, "aliasCN": "梅林", "rarity": 5, "className": "caster"}]
        accepted = [{"skill_name": "lookup_servant", "params": {"name": "梅林"}}]
        clarification = executor._build_multi_candidate_clarification(results, accepted, domain="servant")
        assert clarification is None

    def test_build_filter_relaxation_clarification(self):
        """_build_filter_relaxation_clarification 正确生成放宽条件选项。"""
        from server.skills.executor import SkillExecutor

        executor = SkillExecutor()
        accepted = [
            {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
            {"skill_name": "search_by_class", "params": {"className": "Caster"}},
            {"skill_name": "search_by_effect", "params": {"effect": "npCharge", "minValue": 50}},
        ]
        clarification = executor._build_filter_relaxation_clarification(accepted, domain="servant")
        assert clarification is not None
        assert clarification["type"] == "empty_result_filter"
        option_ids = [o["id"] for o in clarification["options"]]
        assert "drop:search_by_rarity" in option_ids
        assert "drop:search_by_class" in option_ids
        assert "drop_min:search_by_effect" in option_ids

    def test_build_empty_result_clarification_name_query(self):
        """名称查询空结果返回 CLARIFICATION_EMPTY_NAME 类型。"""
        from server.skills.executor import CLARIFICATION_EMPTY_NAME, SkillExecutor

        executor = SkillExecutor()
        accepted = [{"skill_name": "lookup_servant", "params": {"name": "不存在的从者"}}]
        clarification = executor._build_empty_result_clarification(accepted, domain="servant")
        assert clarification is not None
        assert clarification["type"] == CLARIFICATION_EMPTY_NAME
        assert clarification["query_name"] == "不存在的从者"

    def test_build_empty_result_clarification_filter_query(self):
        """筛选查询空结果返回 CLARIFICATION_EMPTY_FILTER 类型。"""
        from server.skills.executor import CLARIFICATION_EMPTY_FILTER, SkillExecutor

        executor = SkillExecutor()
        accepted = [
            {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
        ]
        clarification = executor._build_empty_result_clarification(accepted, domain="servant")
        assert clarification is not None
        assert clarification["type"] == CLARIFICATION_EMPTY_FILTER
