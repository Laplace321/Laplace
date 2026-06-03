"""Tests for search_by_skill_cd Skill."""

import pytest

from server.skills.query.search_by_skill_cd import SearchBySkillCd


@pytest.fixture
def skill():
    return SearchBySkillCd()


def _make_servant(skill_details: list[dict]) -> dict:
    """构造带 skillDetails 的最小从者数据。"""
    return {"skillDetails": skill_details}


def _make_skill(cool_down: int, effects: list[dict] | None = None) -> dict:
    return {
        "skillId": 1,
        "skillName": "test",
        "skillNum": 1,
        "coolDown": cool_down,
        "effects": effects or [],
    }


class TestPureCdFilter:
    """纯 CD 筛选（不带 effect 参数）。"""

    def test_cd_lt_match(self, skill):
        servant = _make_servant([_make_skill(4)])
        assert skill.filter(servant, {"op": "lt", "value": 5}) is True

    def test_cd_lt_no_match(self, skill):
        servant = _make_servant([_make_skill(5)])
        assert skill.filter(servant, {"op": "lt", "value": 5}) is False

    def test_cd_lte_match(self, skill):
        servant = _make_servant([_make_skill(5)])
        assert skill.filter(servant, {"op": "lte", "value": 5}) is True

    def test_cd_eq_match(self, skill):
        servant = _make_servant([_make_skill(5)])
        assert skill.filter(servant, {"op": "eq", "value": 5}) is True

    def test_cd_eq_no_match(self, skill):
        servant = _make_servant([_make_skill(4)])
        assert skill.filter(servant, {"op": "eq", "value": 5}) is False

    def test_partial_match_any_skill(self, skill):
        """三个技能 CD 分别为 7,6,4，查 CD<5 应命中（技能3满足）。"""
        servant = _make_servant(
            [
                _make_skill(7),
                _make_skill(6),
                _make_skill(4),
            ]
        )
        assert skill.filter(servant, {"op": "lt", "value": 5}) is True

    def test_all_above_threshold(self, skill):
        """所有技能 CD > 阈值。"""
        servant = _make_servant(
            [
                _make_skill(7),
                _make_skill(8),
                _make_skill(6),
            ]
        )
        assert skill.filter(servant, {"op": "lt", "value": 5}) is False

    def test_empty_skill_details(self, skill):
        servant = _make_servant([])
        assert skill.filter(servant, {"op": "lt", "value": 5}) is False

    def test_missing_value_returns_true(self, skill):
        """value 缺失时应通过（不做筛选）。"""
        servant = _make_servant([_make_skill(7)])
        assert skill.filter(servant, {"op": "lt"}) is True


class TestEffectCdJoint:
    """效果+CD 联合匹配（同一技能粒度内）。"""

    def test_same_skill_match(self, skill):
        """技能1有 gainNp(self)+CD=4 → 查"自充CD<5"应命中。"""
        servant = _make_servant(
            [
                _make_skill(4, [{"type": "gainNp", "targetType": "self"}]),
            ]
        )
        assert (
            skill.filter(
                servant,
                {
                    "op": "lt",
                    "value": 5,
                    "effect": "gainNp",
                    "target_type": "self",
                },
            )
            is True
        )

    def test_different_skill_no_match(self, skill):
        """技能1有 gainNp+CD=7，技能2无 gainNp+CD=3 → 查"自充CD<5"不命中。"""
        servant = _make_servant(
            [
                _make_skill(7, [{"type": "gainNp", "targetType": "self"}]),
                _make_skill(3, [{"type": "upAtk", "targetType": "party"}]),
            ]
        )
        assert (
            skill.filter(
                servant,
                {
                    "op": "lt",
                    "value": 5,
                    "effect": "gainNp",
                    "target_type": "self",
                },
            )
            is False
        )

    def test_target_type_mismatch(self, skill):
        """技能有 gainNp(party)+CD=3 → 查 targetType=self 应不命中。"""
        servant = _make_servant(
            [
                _make_skill(3, [{"type": "gainNp", "targetType": "party"}]),
            ]
        )
        assert (
            skill.filter(
                servant,
                {
                    "op": "lt",
                    "value": 5,
                    "effect": "gainNp",
                    "target_type": "self",
                },
            )
            is False
        )

    def test_target_type_none_matches_any(self, skill):
        """不指定 targetType 时，只要效果名匹配即可。"""
        servant = _make_servant(
            [
                _make_skill(3, [{"type": "gainNp", "targetType": "party"}]),
            ]
        )
        assert (
            skill.filter(
                servant,
                {
                    "op": "lt",
                    "value": 5,
                    "effect": "gainNp",
                },
            )
            is True
        )

    def test_effect_not_found(self, skill):
        """技能没有目标效果，CD 满足也不命中。"""
        servant = _make_servant(
            [
                _make_skill(3, [{"type": "upAtk", "targetType": "self"}]),
            ]
        )
        assert (
            skill.filter(
                servant,
                {
                    "op": "lt",
                    "value": 5,
                    "effect": "gainNp",
                },
            )
            is False
        )

    def test_multiple_effects_on_same_skill(self, skill):
        """同一技能有多个效果，其中一个匹配即可。"""
        servant = _make_servant(
            [
                _make_skill(
                    4,
                    [
                        {"type": "upAtk", "targetType": "self"},
                        {"type": "gainNp", "targetType": "self"},
                    ],
                ),
            ]
        )
        assert (
            skill.filter(
                servant,
                {
                    "op": "lt",
                    "value": 5,
                    "effect": "gainNp",
                    "target_type": "self",
                },
            )
            is True
        )


class TestEdgeCases:
    """边界情况。"""

    def test_cooldown_zero_skipped(self, skill):
        """coolDown=0 的技能应被跳过（无效值）。"""
        servant = _make_servant([_make_skill(0)])
        assert skill.filter(servant, {"op": "lte", "value": 5}) is False

    def test_cooldown_missing_defaults_zero(self, skill):
        """coolDown 字段缺失时默认 0，应被跳过。"""
        servant = _make_servant(
            [
                {
                    "skillId": 1,
                    "skillName": "test",
                    "skillNum": 1,
                    "effects": [],
                }
            ]
        )
        assert skill.filter(servant, {"op": "lte", "value": 5}) is False

    def test_invalid_op_returns_false(self, skill):
        """无效操作符不应匹配。"""
        servant = _make_servant([_make_skill(3)])
        assert skill.filter(servant, {"op": "invalid", "value": 5}) is False

    def test_gte_operator(self, skill):
        """gte 操作符。"""
        servant = _make_servant([_make_skill(7)])
        assert skill.filter(servant, {"op": "gte", "value": 7}) is True
        assert skill.filter(servant, {"op": "gte", "value": 8}) is False

    def test_gt_operator(self, skill):
        """gt 操作符。"""
        servant = _make_servant([_make_skill(7)])
        assert skill.filter(servant, {"op": "gt", "value": 6}) is True
        assert skill.filter(servant, {"op": "gt", "value": 7}) is False
