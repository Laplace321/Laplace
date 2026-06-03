"""Tests for search_by_effect maxCd (CD联合匹配) functionality."""

import pytest

from server.skills.query.search_by_effect import SearchByEffect


@pytest.fixture
def skill():
    return SearchByEffect()


def _make_skill(cool_down: int, effects: list[dict] | None = None) -> dict:
    return {
        "skillId": 1,
        "skillName": "test",
        "skillNum": 1,
        "coolDown": cool_down,
        "effects": effects or [],
    }


def _make_servant(
    skill_details: list[dict] | None = None,
    skill_effects: list[str] | None = None,
) -> dict:
    """构造带 skillDetails 的最小从者数据。"""
    servant: dict = {"skillDetails": skill_details or []}
    if skill_effects is not None:
        servant["skillEffects"] = skill_effects
    return servant


class TestPureCdFilter:
    """纯 CD 查询（effect=None + maxCd）。"""

    def test_cd_match(self, skill):
        servant = _make_servant([_make_skill(4)])
        assert skill.filter(servant, {"max_cd": 5}) is True

    def test_cd_exact_boundary(self, skill):
        servant = _make_servant([_make_skill(5)])
        assert skill.filter(servant, {"max_cd": 5}) is True

    def test_cd_exceed(self, skill):
        servant = _make_servant([_make_skill(6)])
        assert skill.filter(servant, {"max_cd": 5}) is False

    def test_any_skill_match(self, skill):
        """三个技能 CD 分别为 7,6,4，查 maxCd=5 应命中（技能3满足）。"""
        servant = _make_servant([_make_skill(7), _make_skill(6), _make_skill(4)])
        assert skill.filter(servant, {"max_cd": 5}) is True

    def test_all_above_threshold(self, skill):
        servant = _make_servant([_make_skill(7), _make_skill(8), _make_skill(6)])
        assert skill.filter(servant, {"max_cd": 5}) is False

    def test_empty_skill_details(self, skill):
        servant = _make_servant([])
        assert skill.filter(servant, {"max_cd": 5}) is False

    def test_cooldown_zero_skipped(self, skill):
        servant = _make_servant([_make_skill(0)])
        assert skill.filter(servant, {"max_cd": 5}) is False


class TestEffectCdJoint:
    """效果+CD 联合匹配（同一技能粒度内）。"""

    def test_same_skill_match(self, skill):
        """技能有 gainNp(self)+CD=4 → 查 maxCd=5 应命中。"""
        servant = _make_servant(
            [_make_skill(4, [{"type": "gainNp", "targetType": "self", "valueMax": 5000}])],
            skill_effects=["gainNp"],
        )
        assert skill.filter(servant, {"effect": "gainNp", "target_type": "self", "max_cd": 5}) is True

    def test_different_skill_no_match(self, skill):
        """技能1有 gainNp+CD=7，技能2无 gainNp+CD=3 → 不命中。"""
        servant = _make_servant(
            [
                _make_skill(7, [{"type": "gainNp", "targetType": "self", "valueMax": 5000}]),
                _make_skill(3, [{"type": "upAtk", "targetType": "party", "valueMax": 500}]),
            ],
            skill_effects=["gainNp", "upAtk"],
        )
        assert skill.filter(servant, {"effect": "gainNp", "target_type": "self", "max_cd": 5}) is False

    def test_target_type_mismatch(self, skill):
        """技能有 gainNp(party)+CD=3 → 查 targetType=self 不命中。"""
        servant = _make_servant(
            [_make_skill(3, [{"type": "gainNp", "targetType": "party", "valueMax": 3000}])],
            skill_effects=["gainNp"],
        )
        assert skill.filter(servant, {"effect": "gainNp", "target_type": "self", "max_cd": 5}) is False

    def test_target_type_none_matches_any(self, skill):
        """不指定 targetType 时，只要效果名匹配即可。"""
        servant = _make_servant(
            [_make_skill(3, [{"type": "gainNp", "targetType": "party", "valueMax": 3000}])],
            skill_effects=["gainNp"],
        )
        assert skill.filter(servant, {"effect": "gainNp", "max_cd": 5}) is True


class TestEffectValueCdTriple:
    """效果+数值+CD 三维联合（久远寺有珠场景）。"""

    def test_kuonji_alice_excluded(self, skill):
        """久远寺有珠：技能1 50%自充 CD=6，技能3 20%自充 CD=2。
        查"自充≥50% 且 CD≤5"时不应命中（技能1 CD=6 不满足，技能3 数值=20% 不满足）。
        gainNp valueMax=5000 表示 50%（base=100），minValue=50 → 50*100=5000。
        """
        servant = _make_servant(
            [
                _make_skill(6, [{"type": "gainNp", "targetType": "self", "valueMax": 5000}]),
                _make_skill(6, [{"type": "upAtk", "targetType": "party", "valueMax": 300}]),
                _make_skill(2, [{"type": "gainNp", "targetType": "self", "valueMax": 2000}]),
            ],
            skill_effects=["gainNp", "upAtk"],
        )
        assert (
            skill.filter(
                servant,
                {"effect": "gainNp", "target_type": "self", "min_value": 50, "max_cd": 5},
            )
            is False
        )

    def test_value_and_cd_both_match(self, skill):
        """技能有 gainNp(self) 50%+CD=4 → 三维联合全部满足。"""
        servant = _make_servant(
            [_make_skill(4, [{"type": "gainNp", "targetType": "self", "valueMax": 5000}])],
            skill_effects=["gainNp"],
        )
        assert (
            skill.filter(
                servant,
                {"effect": "gainNp", "target_type": "self", "min_value": 50, "max_cd": 5},
            )
            is True
        )

    def test_value_match_cd_fail(self, skill):
        """数值满足但 CD 超标。"""
        servant = _make_servant(
            [_make_skill(7, [{"type": "gainNp", "targetType": "self", "valueMax": 5000}])],
            skill_effects=["gainNp"],
        )
        assert (
            skill.filter(
                servant,
                {"effect": "gainNp", "target_type": "self", "min_value": 50, "max_cd": 5},
            )
            is False
        )

    def test_cd_match_value_fail(self, skill):
        """CD 满足但数值不足。"""
        servant = _make_servant(
            [_make_skill(3, [{"type": "gainNp", "targetType": "self", "valueMax": 2000}])],
            skill_effects=["gainNp"],
        )
        assert (
            skill.filter(
                servant,
                {"effect": "gainNp", "target_type": "self", "min_value": 50, "max_cd": 5},
            )
            is False
        )


class TestSourceNpIgnoresCd:
    """source=np 时 maxCd 无意义，应忽略 CD 走标准路径。"""

    def test_np_source_ignores_max_cd(self, skill):
        """宝具没有 CD，source=np 时 maxCd 应被忽略。"""
        servant = _make_servant(skill_effects=["gainNp"])
        servant["npEffects"] = ["gainNp"]
        servant["npDetails"] = [
            {
                "npId": 1,
                "npName": "test",
                "effects": [{"type": "gainNp", "targetType": "self", "valueLv1": 3000}],
            }
        ]
        assert (
            skill.filter(
                servant,
                {"effect": "gainNp", "source": "np", "max_cd": 5},
            )
            is True
        )


class TestNoMaxCdUnchanged:
    """maxCd 无值时，行为与之前完全一致。"""

    def test_standard_effect_filter(self, skill):
        """无 maxCd 时走标准跨技能累加路径。"""
        servant = _make_servant(
            [
                _make_skill(7, [{"type": "gainNp", "targetType": "self", "valueMax": 3000}]),
                _make_skill(8, [{"type": "gainNp", "targetType": "self", "valueMax": 2000}]),
            ],
            skill_effects=["gainNp"],
        )
        # 两技能 valueMax 累加 3000+2000=5000（50%），minValue=50 → 5000，应命中
        assert skill.filter(servant, {"effect": "gainNp", "target_type": "self", "min_value": 50}) is True

    def test_no_params_returns_true(self, skill):
        """无任何参数时返回 True（不做筛选）。"""
        servant = _make_servant([_make_skill(7)])
        assert skill.filter(servant, {}) is True
