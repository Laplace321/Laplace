"""Tests for coronation_team Skill."""

import pytest

from server.skills.base import SKILL_REGISTRY


@pytest.fixture
def skill():
    """获取已注册的 coronation_team Skill 实例。"""
    s = SKILL_REGISTRY.get("coronation_team")
    assert s is not None, "coronation_team 未注册到 SKILL_REGISTRY"
    return s


class TestCoronationTeamSkill:
    """戴冠战配队推荐 Skill 测试。"""

    def test_skill_registered(self, skill):
        """Skill 已正确注册。"""
        assert skill.name == "coronation_team"
        assert skill.domain == "coronation"

    def test_load_saber_team(self, skill):
        """加载剑阶配队数据。"""
        result = skill.execute([], {"className": "剑"})
        assert len(result) == 1
        data = result[0]
        assert data["type"] == "team"
        assert data["className"] == "saber"
        assert len(data["playstyles"]) == 2
        assert len(data["roleCategories"]) == 4

    def test_filter_by_role(self, skill):
        """按角色分类过滤。"""
        result = skill.execute([], {"className": "剑", "role": "打手"})
        assert len(result) == 1
        data = result[0]
        assert len(data["roleCategories"]) == 1
        assert data["roleCategories"][0]["role"] == "打手"
        assert len(data["roleCategories"][0]["servants"]) >= 3

    def test_filter_by_playstyle(self, skill):
        """按流派过滤。"""
        result = skill.execute([], {"className": "剑", "playstyle": "双宝具"})
        assert len(result) == 1
        data = result[0]
        assert len(data["playstyles"]) == 1
        assert "双宝具" in data["playstyles"][0]["name"]

    def test_no_role_returns_all_categories(self, skill):
        """未指定 role 时返回全部分类。"""
        result = skill.execute([], {"className": "剑"})
        assert len(result) == 1
        data = result[0]
        roles = [rc["role"] for rc in data["roleCategories"]]
        assert "出星辅助" in roles
        assert "充能辅助" in roles
        assert "增伤辅助" in roles
        assert "打手" in roles

    def test_boss_summary_attached(self, skill):
        """自动附带 Boss 机制摘要。"""
        result = skill.execute([], {"className": "剑"})
        assert len(result) == 1
        data = result[0]
        assert "bossSummary" in data
        assert data["bossSummary"]["bossName"] == "冠位武藏"

    def test_class_not_found(self, skill):
        """未收录职阶返回友好提示。"""
        result = skill.execute([], {"className": "弓"})
        assert len(result) == 1
        assert result[0]["type"] == "not_found"
        assert "暂未收录" in result[0]["message"]

    def test_filter_role_charging(self, skill):
        """过滤充能辅助分类。"""
        result = skill.execute([], {"className": "剑", "role": "充能辅助"})
        assert len(result) == 1
        data = result[0]
        assert len(data["roleCategories"]) == 1
        assert data["roleCategories"][0]["role"] == "充能辅助"
        servants = data["roleCategories"][0]["servants"]
        names = [s["servantName"] for s in servants]
        assert "花嫁尼禄" in names
