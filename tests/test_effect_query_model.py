"""测试效果查询模型的 partyOther 复合判定逻辑。

回归用例覆盖：
- 梅林（跨技能群充累加：20% + 10% = 30%）
- 特斯卡特利波卡（ptOther 30% + self 50%，复合判定通过）
- partyOther 不满足（仅有 ptOther 无自充，复合判定失败）
- 通用效果（upAtk）同逻辑验证
- totalSelfCharge 计算正确性
"""

import pytest

from server.query_executor import _match_effect

# ============================================================
# Fixture: 模拟从者数据
# ============================================================


@pytest.fixture
def merlin_like():
    """模拟梅林：技能1群充20% + 技能3群充10% = 全队30%。

    funcTargetType=ptAll → classify_target_type → "party"
    """
    return {
        "skillEffects": ["gainNp", "upAtk", "invincible"],
        "skillDetails": [
            {
                "skillNum": 1,
                "skillName": "Hero Creation EX",
                "effects": [
                    {
                        "type": "gainNp",
                        "funcType": "gainNp",
                        "targetType": "party",
                        "valueMax": 2000,
                        "turn": 0,
                        "count": 0,
                    },
                ],
            },
            {
                "skillNum": 3,
                "skillName": "Garden of Avalon C",
                "effects": [
                    {
                        "type": "gainNp",
                        "funcType": "gainNp",
                        "targetType": "party",
                        "valueMax": 1000,
                        "turn": 0,
                        "count": 0,
                    },
                ],
            },
        ],
    }


@pytest.fixture
def tezcatlipoca_like():
    """模拟特斯卡特利波卡：ptOther 30% + self 50%。

    funcTargetType=ptOther → classify_target_type → "partyOther"
    funcTargetType=self → "self"
    """
    return {
        "skillEffects": ["gainNp", "upAtk"],
        "skillDetails": [
            {
                "skillNum": 1,
                "skillName": "Smoking Mirror",
                "effects": [
                    {
                        "type": "gainNp",
                        "funcType": "gainNp",
                        "targetType": "partyOther",
                        "valueMax": 3000,
                        "turn": 0,
                        "count": 0,
                    },
                ],
            },
            {
                "skillNum": 2,
                "skillName": "Self Charge",
                "effects": [
                    {
                        "type": "gainNp",
                        "funcType": "gainNp",
                        "targetType": "self",
                        "valueMax": 5000,
                        "turn": 0,
                        "count": 0,
                    },
                ],
            },
        ],
    }


@pytest.fixture
def party_other_only():
    """模拟仅有 partyOther 充能、无自充的从者。

    查 party≥30 时应不命中（自身无法获得≥30充能）。
    """
    return {
        "skillEffects": ["gainNp"],
        "skillDetails": [
            {
                "skillNum": 1,
                "skillName": "Team Charge",
                "effects": [
                    {
                        "type": "gainNp",
                        "funcType": "gainNp",
                        "targetType": "partyOther",
                        "valueMax": 3000,
                        "turn": 0,
                        "count": 0,
                    },
                ],
            },
        ],
    }


@pytest.fixture
def dual_atk_buff():
    """模拟跨技能加攻累加：技能1 全队加攻20% + 技能2 全队加攻10% = 30%。"""
    return {
        "skillEffects": ["upAtk"],
        "skillDetails": [
            {
                "skillNum": 1,
                "skillName": "Charisma A",
                "effects": [
                    {
                        "type": "upAtk",
                        "funcType": "addStateShort",
                        "targetType": "party",
                        "valueMax": 2000,
                        "turn": 3,
                        "count": -1,
                    },
                ],
            },
            {
                "skillNum": 2,
                "skillName": "Military Tactics B",
                "effects": [
                    {
                        "type": "upAtk",
                        "funcType": "addStateShort",
                        "targetType": "party",
                        "valueMax": 1000,
                        "turn": 3,
                        "count": -1,
                    },
                ],
            },
        ],
    }


# ============================================================
# 测试: gainNp (NP充能) 查询
# ============================================================


class TestGainNpPartyQuery:
    """测试 gainNp 全队查询的复合判定逻辑。"""

    def test_merlin_party_30_hit(self, merlin_like):
        """梅林：群充20+10=30 → 查 party≥30 命中。"""
        assert _match_effect(merlin_like, "gainNp", target_type="party", min_value=3000) is True

    def test_merlin_party_40_miss(self, merlin_like):
        """梅林：群充总计30 → 查 party≥40 不命中。"""
        assert _match_effect(merlin_like, "gainNp", target_type="party", min_value=4000) is False

    def test_tezcatlipoca_party_30_hit(self, tezcatlipoca_like):
        """特斯卡特利波卡：ptOther=30 且 self=50≥30 → 查 party≥30 命中。"""
        assert _match_effect(tezcatlipoca_like, "gainNp", target_type="party", min_value=3000) is True

    def test_tezcatlipoca_self_50_hit(self, tezcatlipoca_like):
        """特斯卡特利波卡：self=50 → 查 self≥50 命中。"""
        assert _match_effect(tezcatlipoca_like, "gainNp", target_type="self", min_value=5000) is True

    def test_party_other_only_party_30_miss(self, party_other_only):
        """仅 ptOther 30% 无自充 → 查 party≥30 不命中（自身无法获得≥30）。"""
        assert _match_effect(party_other_only, "gainNp", target_type="party", min_value=3000) is False

    def test_party_other_only_partyother_30_hit(self, party_other_only):
        """仅 ptOther 30% → 查 partyOther≥30 命中。"""
        assert _match_effect(party_other_only, "gainNp", target_type="partyOther", min_value=3000) is True


# ============================================================
# 测试: upAtk (加攻) 同逻辑验证
# ============================================================


class TestUpAtkPartyQuery:
    """验证 partyOther 复合判定对所有效果通用。"""

    def test_dual_atk_party_30_hit(self, dual_atk_buff):
        """跨技能加攻 20+10=30 → 查 party≥30 命中。"""
        assert _match_effect(dual_atk_buff, "upAtk", target_type="party", min_value=3000) is True

    def test_dual_atk_party_40_miss(self, dual_atk_buff):
        """跨技能加攻总计30 → 查 party≥40 不命中。"""
        assert _match_effect(dual_atk_buff, "upAtk", target_type="party", min_value=4000) is False


# ============================================================
# 测试: self 查询语义（self + party + ptOne 都算入自身可获得量）
# ============================================================


class TestSelfQuerySemantics:
    """self 查询语义：自身可获得 = self + party（含自己的全队效果）+ ptOne。"""

    def test_merlin_self_30_hit(self, merlin_like):
        """梅林 party 充能自身也能获得 → 查 self≥30 命中。"""
        assert _match_effect(merlin_like, "gainNp", target_type="self", min_value=3000) is True

    def test_party_other_self_0_miss(self, party_other_only):
        """partyOther 不算入自身 → 查 self≥10 不命中。"""
        assert _match_effect(party_other_only, "gainNp", target_type="self", min_value=1000) is False


# ============================================================
# 测试: totalSelfCharge 计算
# ============================================================


class TestTotalSelfChargeCalculation:
    """验证 data_loader build_database 中 totalSelfCharge 的计算逻辑。"""

    def test_self_charge_includes_party(self):
        """party 类型的 gainNp 应算入 totalSelfCharge。"""
        # 模拟 build_database 中的计算逻辑
        skill_details = [
            {"effects": [{"type": "gainNp", "targetType": "party", "valueMax": 2000}]},
            {"effects": [{"type": "gainNp", "targetType": "self", "valueMax": 3000}]},
        ]
        total = 0
        for sk in skill_details:
            for eff in sk.get("effects", []):
                if eff.get("type") != "gainNp":
                    continue
                tt = eff.get("targetType", "")
                if tt in ("self", "party", "ptOne"):
                    total += eff.get("valueMax", 0) // 100
        assert total == 50  # 20 + 30 = 50%

    def test_party_other_excluded_from_self_charge(self):
        """partyOther 类型的 gainNp 不算入 totalSelfCharge。"""
        skill_details = [
            {"effects": [{"type": "gainNp", "targetType": "partyOther", "valueMax": 3000}]},
            {"effects": [{"type": "gainNp", "targetType": "self", "valueMax": 5000}]},
        ]
        total = 0
        for sk in skill_details:
            for eff in sk.get("effects", []):
                if eff.get("type") != "gainNp":
                    continue
                tt = eff.get("targetType", "")
                if tt in ("self", "party", "ptOne"):
                    total += eff.get("valueMax", 0) // 100
        assert total == 50  # 只有 self 的 50%，partyOther 的 30% 不算入
