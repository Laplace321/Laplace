"""
Schema 单元测试。

测试 RoutingResponse / SkillCall / FallbackReason 的序列化/反序列化。
"""

import json

import pytest

from server.schemas import (
    FallbackReason,
    RoutingResponse,
    SkillCall,
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
        json_str = '{"skill_calls": [{"skill_name": "search_by_np_charge", "params": {"op": "gte", "value": 50}}], "response_skill": "respond_servant_list"}'
        data = json.loads(json_str)
        resp = RoutingResponse.model_validate(data)
        assert resp.skill_calls[0].params["value"] == 50


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
