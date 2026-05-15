"""Tests for SkillExecutor custom_context integration with coronation Skills."""

import pytest

from server.skills.executor import SkillExecutor


@pytest.fixture
def executor():
    return SkillExecutor()


class TestExecutorCustomContext:
    """SkillExecutor 与 coronation domain 的 custom_context 集成测试。"""

    def test_coronation_knowledge_returns_custom_context(self, executor):
        """coronation_knowledge Skill 执行后 result.custom_context 非空。"""
        skill_calls = [{"skill_name": "coronation_knowledge", "params": {"topic": "机制"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        assert result.custom_context is not None
        assert len(result.custom_context) == 1
        assert result.custom_context[0]["type"] == "guide"
        # 纯知识查询无从者
        assert result.servants == []
        assert result.total_found == 0
        # 不进入 fallback
        assert result.is_fallback is False

    def test_coronation_knowledge_no_fallback_on_empty_servants(self, executor):
        """即使 servants 为空，有 custom_context 时不进入 fallback。"""
        skill_calls = [{"skill_name": "coronation_knowledge", "params": {"topic": "星图"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        assert result.is_fallback is False
        assert result.custom_context is not None
        assert result.fallback_message is None

    def test_coronation_team_returns_servants_and_context(self, executor):
        """coronation_team Skill 执行后同时有 custom_context 和关联从者。"""
        skill_calls = [{"skill_name": "coronation_team", "params": {"className": "剑"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        assert result.custom_context is not None
        assert len(result.custom_context) == 1
        assert result.custom_context[0]["type"] == "team"
        # 有关联从者（从 db 中按 collectionNo 匹配）
        assert result.total_found > 0
        assert len(result.servants) > 0
        assert result.is_fallback is False

    def test_coronation_team_servants_have_valid_fields(self, executor):
        """关联从者来自 servants_db，包含完整从者数据字段。"""
        skill_calls = [{"skill_name": "coronation_team", "params": {"className": "剑"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        for servant in result.servants:
            # 必须有 db 中的标准字段
            assert "id" in servant
            assert "name" in servant
            assert "collectionNo" in servant
            assert "rarity" in servant
            assert "className" in servant

    def test_coronation_team_role_filter_limits_servants(self, executor):
        """按角色过滤时，关联从者也应对应减少。"""
        # 全量
        full_calls = [{"skill_name": "coronation_team", "params": {"className": "剑"}}]
        full_result = executor.execute(full_calls, "respond_coronation")

        # 仅充能辅助
        filtered_calls = [{"skill_name": "coronation_team", "params": {"className": "剑", "role": "充能辅助"}}]
        filtered_result = executor.execute(filtered_calls, "respond_coronation")

        # 过滤后从者数量应该 <= 全量
        assert filtered_result.total_found <= full_result.total_found

    def test_coronation_team_not_found_returns_custom_context(self, executor):
        """未收录职阶返回 custom_context（type=not_found），不进 fallback。"""
        skill_calls = [{"skill_name": "coronation_team", "params": {"className": "弓"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        assert result.is_fallback is False
        assert result.custom_context is not None
        assert result.custom_context[0]["type"] == "not_found"
        assert result.servants == []

    def test_response_skill_resolved_correctly(self, executor):
        """ExecutionResult 正确解析 respond_coronation ResponseSkill。"""
        skill_calls = [{"skill_name": "coronation_knowledge", "params": {"topic": "机制"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        assert result.response_skill is not None
        assert result.response_skill.name == "respond_coronation"

    def test_servants_sorted_by_rarity(self, executor):
        """关联从者按稀有度降序排列。"""
        skill_calls = [{"skill_name": "coronation_team", "params": {"className": "剑"}}]
        result = executor.execute(skill_calls, "respond_coronation")

        if len(result.servants) > 1:
            rarities = [s.get("rarity", 0) for s in result.servants]
            # 验证降序（允许相等）
            for i in range(len(rarities) - 1):
                assert rarities[i] >= rarities[i + 1]
