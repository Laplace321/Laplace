"""效果查询模型 — 效果类型 × 目标类型全组合测试。

覆盖场景：
- gainNp（NP充能）: self / party / partyOther / ptOne
- upAtk（攻击力加成）: self / party / partyOther / ptOne
- regainNp（每回合NP/魔放）: self / party
- upBuster / upArts / upQuick（色卡加成）: self / party / ptOne

每个测试用例验证：
1. 匹配结果包含预期从者（正例）
2. 不满足条件的从者不被匹配（反例）
3. minValue 数值过滤正确生效
"""

import pytest

from server.query_executor import _match_effect, load_database
from server.skills.executor import SkillExecutor


@pytest.fixture(scope="module")
def db():
    return load_database()


@pytest.fixture(scope="module")
def executor():
    return SkillExecutor()


def _find(db, collection_no: int):
    """按 collectionNo 查找从者。"""
    return next((s for s in db if s["collectionNo"] == collection_no), None)


# ============================================================
# 一、gainNp（NP 充能）
# ============================================================


class TestGainNpSelf:
    """gainNp + targetType=self（自身可获得的 NP 充能量）。"""

    def test_castoria_self_50_hit(self, db):
        """卡斯特利亚: party 30% + ptOne 20% → self 可获得 50%。"""
        svt = _find(db, 284)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="self", min_value=5000)

    def test_waver_self_50_hit(self, db):
        """诸葛孔明: ptOne 30% + party 10% + party 10% → self 可获得 50%。"""
        svt = _find(db, 37)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="self", min_value=5000)

    def test_merlin_self_30_hit(self, db):
        """梅林: party 20% + party 10% → self 可获得 30%。"""
        svt = _find(db, 150)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="self", min_value=3000)

    def test_merlin_self_40_miss(self, db):
        """梅林: self 可获得 30%，不满足 ≥40%。"""
        svt = _find(db, 150)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="self", min_value=4000)

    def test_tezcatlipoca_self_50_hit(self, db):
        """特斯卡特利波卡: self 50% → 满足 ≥50%。"""
        svt = _find(db, 371)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="self", min_value=5000)

    def test_executor_self_50(self, executor):
        """通过 SkillExecutor 验证 gainNp self ≥50% 结果的正确性。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "gainNp",
                        "targetType": "self",
                        "minValue": 50,
                        "source": "skill",
                    },
                },
            ],
        )
        assert result.total_found > 0
        nos = [s["collectionNo"] for s in result.servants]
        # 卡斯特利亚(self30+party30→self可获50)和特斯卡(self50)应在结果中
        assert 284 in nos, "卡斯特利亚(50%自充)应被匹配"
        assert 371 in nos, "特斯卡特利波卡(50%自充)应被匹配"


class TestGainNpParty:
    """gainNp + targetType=party（全队含自己可获得的 NP 充能量）。"""

    def test_merlin_party_30_hit(self, db):
        """梅林: party 20% + party 10% = 全队 30%。"""
        svt = _find(db, 150)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="party", min_value=3000)

    def test_castoria_party_30_hit(self, db):
        """卡斯特利亚: party 30%（ptOne 不计入全队，因为只能给一个人）。"""
        svt = _find(db, 284)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="party", min_value=3000)

    def test_castoria_party_50_miss(self, db):
        """卡斯特利亚: party 仅 30%（ptOne 20% 不计入全队），不满足 ≥50%。"""
        svt = _find(db, 284)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="party", min_value=5000)

    def test_tezcatlipoca_party_30_hit(self, db):
        """特斯卡特利波卡: 技能2 self=5000 + partyOther=3000，同技能共存触发重分类。
        partyOther 重分类为 party 后，party 查询累加得 3000（≥30%）。"""
        svt = _find(db, 371)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="party", min_value=3000)

    def test_merlin_party_40_miss(self, db):
        """梅林: 全队总充能 30%，不满足 ≥40%。"""
        svt = _find(db, 150)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="party", min_value=4000)


class TestGainNpPartyOther:
    """gainNp + targetType=partyOther（仅队友可获得的充能量）。

    partyOther 复合重分类规则：
    - 同技能中 partyOther + self 同效果共存 → partyOther 重分类为 party（不再匹配 partyOther 查询）
    - 纯 partyOther（同技能无 self）→ 不触发重分类（仍匹配 partyOther 查询）
    """

    def test_tezcatlipoca_partyother_miss(self, db):
        """特斯卡特利波卡: 技能2 gainNp self=5000 + partyOther=3000，触发重分类。
        partyOther 被重分类为 party，partyOther 查询不再匹配。"""
        svt = _find(db, 371)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="partyOther", min_value=1000)

    def test_larva_tiamat_partyother_miss(self, db):
        """幼体提亚马特: 技能2 gainNp self=5000 + partyOther=3000，触发重分类。
        partyOther 被重分类为 party，partyOther 查询不再匹配。"""
        svt = _find(db, 450)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="partyOther", min_value=1000)

    def test_jason_partyother_20_hit(self, db):
        """伊阿宋: 纯 partyOther 20%（同技能无 self），不触发重分类。"""
        svt = _find(db, 254)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="partyOther", min_value=2000)

    def test_jason_partyother_30_miss(self, db):
        """伊阿宋: partyOther 只有 20%，不满足 ≥30%。"""
        svt = _find(db, 254)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="partyOther", min_value=3000)

    def test_merlin_partyother_miss(self, db):
        """梅林: 没有 partyOther 效果（只有 party），不应匹配。"""
        svt = _find(db, 150)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="partyOther", min_value=1000)

    def test_executor_partyother_20(self, executor):
        """通过 SkillExecutor 验证 gainNp partyOther ≥20%。
        伊阿宋(纯partyOther)应在结果中，特斯卡特利波卡(重分类)不在。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "gainNp",
                        "targetType": "partyOther",
                        "minValue": 20,
                        "source": "skill",
                    },
                },
            ],
        )
        assert result.total_found > 0
        nos = [s["collectionNo"] for s in result.servants]
        assert 254 in nos, "伊阿宋(纯partyOther 20%)应被匹配"
        assert 371 not in nos, "特斯卡特利波卡(重分类后)不应被partyOther匹配"


class TestGainNpPtOne:
    """gainNp + targetType=ptOne（单体队友充能）。"""

    def test_nero_bride_ptone_30_hit(self, db):
        """尼禄新娘: ptOne 30%。"""
        svt = _find(db, 90)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="ptOne", min_value=3000)

    def test_waver_ptone_30_hit(self, db):
        """诸葛孔明: ptOne 30%。"""
        svt = _find(db, 37)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="ptOne", min_value=3000)

    def test_castoria_ptone_20_hit(self, db):
        """卡斯特利亚: ptOne 20%。"""
        svt = _find(db, 284)
        assert svt is not None
        assert _match_effect(svt, "gainNp", target_type="ptOne", min_value=2000)

    def test_castoria_ptone_50_hit(self, db):
        """卡斯特利亚: party 30% + ptOne 20% → 单体队友可获得 50%（party 也惠及队友）。"""
        svt = _find(db, 284)
        assert svt is not None
        # ptOne 查询累加 party + ptOne，因为 party 效果也能给到单体队友
        assert _match_effect(svt, "gainNp", target_type="ptOne", min_value=5000)

    def test_castoria_ptone_60_miss(self, db):
        """卡斯特利亚: 单体队友可获得 50%，不满足 ≥60%。"""
        svt = _find(db, 284)
        assert svt is not None
        assert not _match_effect(svt, "gainNp", target_type="ptOne", min_value=6000)


# ============================================================
# 二、upAtk（攻击力加成）
# ============================================================


class TestUpAtkSelf:
    """upAtk + targetType=self（自身攻击力加成）。"""

    def test_nero_self_44_hit(self, db):
        """尼禄: self upAtk valueMax=440（44%）。"""
        svt = _find(db, 5)
        assert svt is not None
        # 440 内部值 = 44%，查 ≥40% → min_value=400
        assert _match_effect(svt, "upAtk", target_type="self", min_value=400)

    def test_siegfried_self_30_hit(self, db):
        """齐格飞: self upAtk valueMax=300（30%）。"""
        svt = _find(db, 6)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="self", min_value=300)

    def test_siegfried_self_40_miss(self, db):
        """齐格飞: self upAtk 30%，不满足 ≥40%。"""
        svt = _find(db, 6)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="self", min_value=400)


class TestUpAtkParty:
    """upAtk + targetType=party（全队攻击力加成）。"""

    def test_elisabeth_brave_party_50_hit(self, db):
        """伊丽莎白勇者: party upAtk 50%。"""
        svt = _find(db, 138)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="party", min_value=500)

    def test_lanling_party_20_hit(self, db):
        """兰陵王: party upAtk 20%。"""
        svt = _find(db, 227)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="party", min_value=200)

    def test_lanling_party_30_miss(self, db):
        """兰陵王: party upAtk 20%，不满足 ≥30%。"""
        svt = _find(db, 227)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="party", min_value=300)

    def test_space_ishtar_party_30_hit(self, db):
        """太空伊什塔尔: 技能1 self=200 + partyOther=300，重分类后 partyOther→party。
        party 查询累加得 300（≥30%）。"""
        svt = _find(db, 268)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="party", min_value=300)

    def test_space_ishtar_party_40_miss(self, db):
        """太空伊什塔尔: 重分类后 party=300，不满足 ≥40%。"""
        svt = _find(db, 268)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="party", min_value=400)

    def test_calamity_jane_party_20_hit(self, db):
        """Calamity Jane: 技能2 self=200 + partyOther=200，重分类后 partyOther→party。
        party 查询累加得 200（≥20%）。"""
        svt = _find(db, 269)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="party", min_value=200)

    def test_calamity_jane_party_30_miss(self, db):
        """Calamity Jane: 重分类后 party=200，不满足 ≥30%。"""
        svt = _find(db, 269)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="party", min_value=300)

    def test_moriarty_party_20_hit(self, db):
        """莫里亚蒂: 技能3 party=200 + partyOther=200 (无 self，不重分类)。
        party 查询累加 party + partyOther = 400，但 partyOther 复合判定要求
        自身也满足阈值（self_total=200 < 400），所以 ≥20% 才能匹配。"""
        svt = _find(db, 156)
        assert svt is not None
        # party=200 可直接满足 ≥20%（无需 partyOther 复合判定）
        assert _match_effect(svt, "upAtk", target_type="party", min_value=200)

    def test_moriarty_party_30_miss(self, db):
        """莫里亚蒂: party=200 + partyOther=200，但 partyOther 复合判定：
        self_total(0+200+0=200) < min_value(300)，判定失败。
        team_value 仍为 400，但自身无法满足 30% 阈值，不匹配。"""
        svt = _find(db, 156)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="party", min_value=300)

    def test_executor_party_atk_20(self, executor):
        """通过 SkillExecutor 验证 upAtk party ≥20%。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "upAtk",
                        "targetType": "party",
                        "minValue": 20,
                        "source": "skill",
                    },
                },
            ],
        )
        assert result.total_found > 0
        # 兰陵王应在结果中
        nos = [s["collectionNo"] for s in result.servants]
        assert 227 in nos
        # 太空伊什塔尔应在结果中（重分类后 partyOther→party）
        assert 268 in nos, "太空伊什塔尔(重分类后30% party upAtk)应被匹配"


class TestUpAtkPartyOther:
    """upAtk + targetType=partyOther（仅队友攻击力加成）。

    partyOther 复合重分类规则：
    - 同技能中 partyOther + self 同效果共存 → partyOther 重分类为 party（不再匹配 partyOther 查询）
    - 同技能中 partyOther + party（无 self）→ 不触发重分类（仍匹配 partyOther 查询）
    """

    def test_moriarty_partyother_40_hit(self, db):
        """莫里亚蒂: 技能3 party=200 + partyOther=200 (无 self)，不触发重分类。
        partyOther 保留 200，可匹配 ≥20%。"""
        svt = _find(db, 156)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="partyOther", min_value=200)

    def test_moriarty_partyother_30_miss(self, db):
        """莫里亚蒂: partyOther 只有 20%，不满足 ≥30%。"""
        svt = _find(db, 156)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="partyOther", min_value=300)

    def test_space_ishtar_partyother_miss(self, db):
        """太空伊什塔尔: 技能1 self=200 + partyOther=300，触发重分类。
        partyOther 被重分类为 party，partyOther 查询不匹配。"""
        svt = _find(db, 268)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="partyOther", min_value=100)

    def test_calamity_jane_partyother_miss(self, db):
        """Calamity Jane: 技能2 self=200 + partyOther=200，触发重分类。
        partyOther 被重分类为 party，partyOther 查询不匹配。"""
        svt = _find(db, 269)
        assert svt is not None
        assert not _match_effect(svt, "upAtk", target_type="partyOther", min_value=100)

    def test_jeanne_partyother_20_hit(self, db):
        """贞德: partyOther upAtk 20%。"""
        svt = _find(db, 216)
        assert svt is not None
        assert _match_effect(svt, "upAtk", target_type="partyOther", min_value=200)


# ============================================================
# 三、regainNp（每回合NP/魔放）
# ============================================================


class TestRegainNpSelf:
    """regainNp + targetType=self（自身每回合NP）。"""

    def test_medb_self_20_hit(self, db):
        """女王梅芙: self regainNp valueMax=2000（20%）。"""
        svt = _find(db, 221)
        assert svt is not None
        assert _match_effect(svt, "regainNp", target_type="self", min_value=2000)

    def test_lord_logres_self_50_hit(self, db):
        """领主洛格雷斯: self regainNp valueMax=5000（50%）。"""
        svt = _find(db, 461)
        assert svt is not None
        assert _match_effect(svt, "regainNp", target_type="self", min_value=5000)

    def test_suzuka_self_10_hit(self, db):
        """铃鹿御前: self regainNp valueMax=1000（10%）。"""
        svt = _find(db, 165)
        assert svt is not None
        assert _match_effect(svt, "regainNp", target_type="self", min_value=1000)

    def test_suzuka_self_20_miss(self, db):
        """铃鹿御前: self regainNp 10%，不满足 ≥20%。"""
        svt = _find(db, 165)
        assert svt is not None
        assert not _match_effect(svt, "regainNp", target_type="self", min_value=2000)

    def test_executor_regainnp_self_10(self, executor):
        """通过 SkillExecutor 验证 regainNp self ≥10%。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "regainNp",
                        "targetType": "self",
                        "minValue": 10,
                        "source": "skill",
                    },
                },
            ],
        )
        assert result.total_found > 0
        # 女王梅芙应在结果中
        nos = [s["collectionNo"] for s in result.servants]
        assert 221 in nos


class TestRegainNpParty:
    """regainNp + targetType=party（全队每回合NP）。"""

    def test_jacques_party_10_hit(self, db):
        """雅克·德·莫莱: party regainNp valueMax=1000（10%）。"""
        svt = _find(db, 466)
        assert svt is not None
        assert _match_effect(svt, "regainNp", target_type="party", min_value=1000)

    def test_sei_shounagon_party_10_hit(self, db):
        """清少纳言: party regainNp valueMax=1000（10%）。"""
        svt = _find(db, 276)
        assert svt is not None
        assert _match_effect(svt, "regainNp", target_type="party", min_value=1000)

    def test_jacques_party_20_miss(self, db):
        """雅克·德·莫莱: party regainNp 10%，不满足 ≥20%。"""
        svt = _find(db, 466)
        assert svt is not None
        assert not _match_effect(svt, "regainNp", target_type="party", min_value=2000)


# ============================================================
# 四、色卡加成（upBuster / upArts / upQuick）
# ============================================================


class TestUpBusterParty:
    """upBuster + targetType=party（全队红卡加成）。"""

    def test_tawara_party_20_hit(self, db):
        """俵藤太: party upBuster 20%。"""
        svt = _find(db, 125)
        assert svt is not None
        assert _match_effect(svt, "upBuster", target_type="party", min_value=200)

    def test_tawara_party_30_miss(self, db):
        """俵藤太: party upBuster 20%，不满足 ≥30%。"""
        svt = _find(db, 125)
        assert svt is not None
        assert not _match_effect(svt, "upBuster", target_type="party", min_value=300)


class TestUpArtsParty:
    """upArts + targetType=party（全队蓝卡加成）。"""

    def test_lanling_party_20_hit(self, db):
        """兰陵王: party upArts 20%。"""
        svt = _find(db, 227)
        assert svt is not None
        assert _match_effect(svt, "upArts", target_type="party", min_value=200)

    def test_lanling_party_30_miss(self, db):
        """兰陵王: party upArts 20%，不满足 ≥30%。"""
        svt = _find(db, 227)
        assert svt is not None
        assert not _match_effect(svt, "upArts", target_type="party", min_value=300)

    def test_executor_arts_party_20(self, executor):
        """通过 SkillExecutor 验证 upArts party ≥20%。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "upArts",
                        "targetType": "party",
                        "minValue": 20,
                        "source": "skill",
                    },
                },
            ],
        )
        assert result.total_found > 0
        nos = [s["collectionNo"] for s in result.servants]
        assert 227 in nos


class TestUpQuickPtOne:
    """upQuick + targetType=ptOne（单体队友绿卡加成）。"""

    def test_skadi_ptone_50_hit(self, db):
        """斯卡蒂: ptOne upQuick valueMax=500（50%）。"""
        svt = _find(db, 215)
        assert svt is not None
        assert _match_effect(svt, "upQuick", target_type="ptOne", min_value=500)

    def test_scathach_ptone_50_hit(self, db):
        """斯卡哈: ptOne upQuick valueMax=500（50%）。"""
        svt = _find(db, 70)
        assert svt is not None
        assert _match_effect(svt, "upQuick", target_type="ptOne", min_value=500)

    def test_skadi_ptone_60_miss(self, db):
        """斯卡蒂: ptOne upQuick 50%，不满足 ≥60%。"""
        svt = _find(db, 215)
        assert svt is not None
        assert not _match_effect(svt, "upQuick", target_type="ptOne", min_value=600)


# ============================================================
# 五、_convert_value 转换系数端到端验证
# ============================================================


class TestConvertValueEndToEnd:
    """验证 SkillExecutor 中 minValue 百分比→内部单位转换的正确性。

    用户传 minValue=50（表示 50%），系统应转换为：
    - gainNp/regainNp: 50×100 = 5000
    - upAtk/upBuster/upArts/upQuick: 50×10 = 500
    """

    def test_gainNp_50_percent_conversion(self, executor):
        """gainNp minValue=50 → 内部 5000（×100 转换）。
        卡斯特利亚(totalSelfCharge=50)应在结果中。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "gainNp",
                        "targetType": "self",
                        "minValue": 50,
                        "source": "skill",
                    },
                },
            ],
        )
        nos = [s["collectionNo"] for s in result.servants]
        assert 284 in nos, "卡斯特利亚(50%自充)应被匹配"
        assert 371 in nos, "特斯卡特利波卡(50%自充)应被匹配"

    def test_upAtk_50_percent_conversion(self, executor):
        """upAtk minValue=50 → 内部 500（×10 转换）。
        伊丽莎白勇者(party 50%)应在结果中。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "upAtk",
                        "targetType": "party",
                        "minValue": 50,
                        "source": "skill",
                    },
                },
            ],
        )
        nos = [s["collectionNo"] for s in result.servants]
        assert 138 in nos, "伊丽莎白勇者(50% party upAtk)应被匹配"

    def test_upQuick_50_percent_conversion(self, executor):
        """upQuick minValue=50 → 内部 500（×10 转换）。
        斯卡蒂(ptOne 50%)应在结果中。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "upQuick",
                        "targetType": "ptOne",
                        "minValue": 50,
                        "source": "skill",
                    },
                },
            ],
        )
        nos = [s["collectionNo"] for s in result.servants]
        assert 215 in nos, "斯卡蒂(50% ptOne upQuick)应被匹配"

    def test_regainNp_10_percent_conversion(self, executor):
        """regainNp minValue=10 → 内部 1000（×100 转换）。
        清少纳言(party 10%)应在结果中。"""
        result = executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {
                        "effect": "regainNp",
                        "targetType": "party",
                        "minValue": 10,
                        "source": "skill",
                    },
                },
            ],
        )
        nos = [s["collectionNo"] for s in result.servants]
        assert 276 in nos, "清少纳言(10% party regainNp)应被匹配"
