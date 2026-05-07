"""
Skill-Based Architecture — Schema 单元测试

测试新 Schema（RoutingResponse、SkillCall、FallbackReason）的序列化/反序列化，
以及 ChatRequest 双模式字段校验。
"""

import pytest
from pydantic import ValidationError

from server.schemas import (
    ChatRequest,
    ChatResponse,
    FallbackReason,
    RoutingResponse,
    SkillCall,
    routing_response_json_schema,
)

# === SkillCall 测试 ===


def test_skill_call_basic():
    """基本 SkillCall 创建和序列化。"""
    sc = SkillCall(skill_name="search_by_class", params={"class_name": "saber"})
    assert sc.skill_name == "search_by_class"
    assert sc.params == {"class_name": "saber"}
    data = sc.model_dump()
    assert data["skill_name"] == "search_by_class"


def test_skill_call_empty_params():
    """SkillCall 空参数默认为空 dict。"""
    sc = SkillCall(skill_name="lookup_servant")
    assert sc.params == {}


def test_skill_call_extra_fields_ignored():
    """SkillCall 忽略额外字段（ConfigDict extra=ignore）。"""
    sc = SkillCall(skill_name="test", params={}, unknown_field="ignored")
    assert sc.skill_name == "test"
    assert not hasattr(sc, "unknown_field")


# === FallbackReason 测试 ===


def test_fallback_reason_types():
    """FallbackReason 三种降级类型。"""
    for fb_type in ["non_game_query", "unsupported_query", "clarification_needed"]:
        fb = FallbackReason(type=fb_type, message="test")
        assert fb.type == fb_type


def test_fallback_reason_invalid_type():
    """FallbackReason 无效类型抛出校验错误。"""
    with pytest.raises(ValidationError):
        FallbackReason(type="invalid_type", message="test")


# === RoutingResponse 测试 ===


def test_routing_response_with_skills():
    """RoutingResponse 包含 Skills 和 Response Skill。"""
    rr = RoutingResponse(
        query_skills=[
            SkillCall(skill_name="search_by_class", params={"class_name": "saber"}),
            SkillCall(skill_name="search_by_rarity", params={"op": "gte", "value": 5}),
        ],
        response_skill="respond_servant_list",
    )
    assert len(rr.query_skills) == 2
    assert rr.response_skill == "respond_servant_list"
    assert rr.fallback is None


def test_routing_response_with_fallback():
    """RoutingResponse 降级场景（无 Skills，有 fallback）。"""
    rr = RoutingResponse(
        query_skills=[],
        fallback=FallbackReason(type="non_game_query", message="这不是游戏问题"),
    )
    assert rr.fallback is not None
    assert rr.fallback.type == "non_game_query"
    assert len(rr.query_skills) == 0


def test_routing_response_roundtrip():
    """RoutingResponse 序列化/反序列化往返。"""
    rr = RoutingResponse(
        query_skills=[SkillCall(skill_name="search_by_np_charge", params={"op": "gte", "value": 30})],
        response_skill="respond_servant_detail",
    )
    data = rr.model_dump()
    rr2 = RoutingResponse(**data)
    assert rr2.query_skills[0].skill_name == "search_by_np_charge"
    assert rr2.response_skill == "respond_servant_detail"


def test_routing_response_json_schema():
    """routing_response_json_schema() 返回有效 JSON Schema。"""
    schema = routing_response_json_schema()
    assert "properties" in schema
    assert "query_skills" in schema["properties"]
    assert "response_skill" in schema["properties"]


# === ChatRequest 双模式测试 ===


def test_chat_request_natural_language_mode():
    """natural_language 模式：基本创建。"""
    req = ChatRequest(message="五星剑阶从者")
    assert req.mode == "natural_language"
    assert req.message == "五星剑阶从者"
    assert req.preset_name is None


def test_chat_request_preset_mode():
    """preset 模式：含 preset_name、params、supplement。"""
    req = ChatRequest(
        mode="preset",
        preset_name="cycle_farming",
        params={"search_by_np_charge": {"op": "gte", "value": 30}},
        supplement="只要五星的",
        response_skill="respond_servant_list",
    )
    assert req.mode == "preset"
    assert req.preset_name == "cycle_farming"
    assert req.params["search_by_np_charge"]["op"] == "gte"
    assert req.supplement == "只要五星的"


def test_chat_request_preset_empty_message():
    """preset 模式允许 message 为空。"""
    req = ChatRequest(mode="preset", preset_name="servant_lookup")
    assert req.message == ""


def test_chat_request_invalid_mode():
    """无效 mode 抛出校验错误。"""
    with pytest.raises(ValidationError):
        ChatRequest(mode="invalid_mode", message="test")


# === ChatResponse 测试 ===


def test_chat_response_basic():
    """基本 ChatResponse 创建。"""
    resp = ChatResponse(
        reply="找到了 3 位从者",
        servants=[{"id": 1, "name": "test"}],
        count=1,
        query={"class_name": "saber"},
        model="gpt-4",
        traceId="abc12345",
    )
    assert resp.reply == "找到了 3 位从者"
    assert resp.count == 1
    assert resp.traceId == "abc12345"
