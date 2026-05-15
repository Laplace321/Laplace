"""Tests for coronation_knowledge Skill."""

import pytest

from server.skills.base import SKILL_REGISTRY


@pytest.fixture
def skill():
    """获取已注册的 coronation_knowledge Skill 实例。"""
    s = SKILL_REGISTRY.get("coronation_knowledge")
    assert s is not None, "coronation_knowledge 未注册到 SKILL_REGISTRY"
    return s


class TestCoronationKnowledgeSkill:
    """戴冠战知识检索 Skill 测试。"""

    def test_skill_registered(self, skill):
        """Skill 已正确注册。"""
        assert skill.name == "coronation_knowledge"
        assert skill.domain == "coronation"

    def test_guide_topic_match(self, skill):
        """按 topic 精确匹配知识条目。"""
        result = skill.execute([], {"topic": "星图"})
        assert len(result) == 1
        assert result[0]["type"] == "guide"
        entries = result[0]["entries"]
        assert len(entries) == 1
        assert entries[0]["id"] == "star_chart_unlock"

    def test_guide_topic_mechanism(self, skill):
        """匹配机制条目。"""
        result = skill.execute([], {"topic": "机制"})
        assert len(result) == 1
        entries = result[0]["entries"]
        assert len(entries) == 1
        assert entries[0]["id"] == "mechanism_overview"

    def test_guide_no_topic_returns_all(self, skill):
        """无 topic 时返回全部条目。"""
        result = skill.execute([], {})
        assert len(result) == 1
        assert result[0]["type"] == "guide"
        entries = result[0]["entries"]
        assert len(entries) == 4  # 机制/星图/礼装/刷取

    def test_boss_saber(self, skill):
        """查询剑阶 Boss 机制。"""
        result = skill.execute([], {"topic": "boss", "className": "剑"})
        assert len(result) == 1
        assert result[0]["type"] == "boss"
        data = result[0]["data"]
        assert data["bossName"] == "冠位武藏"
        assert "神性" in data["traits"]

    def test_boss_not_found(self, skill):
        """查询未收录职阶 Boss 返回友好提示。"""
        result = skill.execute([], {"topic": "boss", "className": "弓"})
        assert len(result) == 1
        assert result[0]["type"] == "not_found"
        assert "暂未收录" in result[0]["message"]

    def test_guide_topic_not_matched_returns_all(self, skill):
        """topic 不匹配时返回全部条目作为降级。"""
        result = skill.execute([], {"topic": "不存在的话题"})
        assert len(result) == 1
        entries = result[0]["entries"]
        assert len(entries) == 4
        assert "note" in result[0]
