"""
Skill-Based Architecture — Query Skill 单元测试

将原 test_query_executor.py 的 7 个测试用例逐条迁移到 Skill 粒度。
每个测试直接调用 Skill 实例的 filter() / execute() 方法，保持等价断言覆盖。
"""

from unittest.mock import patch

from server.skills.query.compare_servants import CompareServants
from server.skills.query.lookup_servant import LookupServant
from server.skills.query.search_by_attribute import SearchByAttribute
from server.skills.query.search_by_cards import SearchByCards
from server.skills.query.search_by_class import SearchByClass
from server.skills.query.search_by_np_charge import SearchByNpCharge
from server.skills.query.search_by_np_effect import SearchByNpEffect
from server.skills.query.search_by_rarity import SearchByRarity
from server.skills.query.search_by_skill_effect import SearchBySkillEffect
from server.skills.query.search_by_traits import SearchByTraits

# === 共享测试数据（与原测试完全一致） ===

SERVANTS = [
    {
        "id": 1,
        "collectionNo": 1,
        "name": "Altria Pendragon",
        "originalName": "アルトリア・ペンドラゴン",
        "aliasCN": "阿尔托莉雅·潘德拉贡",
        "rarity": 5,
        "className": "saber",
        "npCharges": [{"chargePercent": 30, "targetType": "self"}],
        "totalCharge": 30,
        "hasNpCharge": True,
        "skillEffects": ["gainNp", "invincible", "upAtk"],
        "npEffects": ["upAtk"],
        "skillDetails": [
            {
                "skillName": "Charisma",
                "effects": [
                    {"type": "upAtk", "targetType": "party"},
                    {"type": "invincible", "targetType": "self"},
                ],
            }
        ],
        "traits": [300, 303, 2002],
        "gender": "female",
        "attribute": "earth",
        "cards": {"buster": 2, "arts": 2, "quick": 1},
        "npCard": "buster",
        "npTarget": "all",
    },
    {
        "id": 2,
        "collectionNo": 2,
        "name": "James Moriarty",
        "originalName": "ジェームズ・モリアーティ",
        "aliasCN": "詹姆斯·莫里亚蒂",
        "rarity": 5,
        "className": "ruler",
        "npCharges": [{"chargePercent": 50, "targetType": "ptOne"}],
        "totalCharge": 50,
        "hasNpCharge": True,
        "skillEffects": ["gainNp", "avoidance", "guts"],
        "npEffects": ["invincible", "upAtk"],
        "skillDetails": [
            {
                "skillName": "Escape",
                "effects": [
                    {"type": "avoidance", "targetType": "self"},
                    {"type": "guts", "targetType": "self"},
                ],
            }
        ],
        "traits": [301, 304],
        "gender": "male",
        "attribute": "human",
        "cards": {"buster": 1, "arts": 3, "quick": 1},
        "npCard": "arts",
        "npTarget": "one",
    },
    {
        "id": 3,
        "collectionNo": 3,
        "name": "Hans Christian Andersen",
        "originalName": "アンデルセン",
        "aliasCN": "汉斯·克里斯蒂安·安徒生",
        "rarity": 2,
        "className": "caster",
        "npCharges": [],
        "totalCharge": 0,
        "hasNpCharge": False,
        "skillEffects": ["upCriticaldamage"],
        "npEffects": [],
        "skillDetails": [],
        "traits": [302],
        "gender": "male",
        "attribute": "human",
        "cards": {"buster": 1, "arts": 3, "quick": 1},
        "npCard": "arts",
        "npTarget": "support",
    },
    {
        "id": 4,
        "collectionNo": 4,
        "name": "Altria Caster",
        "originalName": "アルトリア・キャスター",
        "aliasCN": "阿尔托莉雅·卡斯特",
        "rarity": 5,
        "className": "berserker",
        "npCharges": [
            {"chargePercent": 30, "targetType": "ptAll"},
            {"chargePercent": 20, "targetType": "ptOne"},
        ],
        "totalCharge": 50,
        "hasNpCharge": True,
        "skillEffects": ["gainNp", "upArts"],
        "npEffects": ["upArts", "gainNp"],
        "skillDetails": [],
        "traits": [308],
        "gender": "female",
        "attribute": "star",
        "cards": {"buster": 1, "arts": 3, "quick": 1},
        "npCard": "arts",
        "npTarget": "support",
    },
]

NICKNAMES = {
    "呆毛": {"name": "阿尔托莉雅·潘德拉贡", "className": "saber"},
    "小教授": {"name": "詹姆斯·莫里亚蒂", "className": "ruler"},
    "水C呆": {"name": "阿尔托莉雅·卡斯特", "className": "berserker"},
    "泳装阿尔托莉雅": {"name": "阿尔托莉雅·卡斯特", "className": "berserker"},
}


def names(results):
    return [s["name"] for s in results]


def execute_skill(skill, db, params):
    """辅助：用 Skill 实例过滤 db，按稀有度降序+collectionNo 升序排序。"""
    results = skill.execute(db, params)
    results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))
    return results


# === 等价测试用例（对应原 test_query_executor.py 的 7 个测试） ===


def test_np_charge_exact_and_gte_filters():
    """迁移自 test_np_charge_exact_and_gte_filters。"""
    skill = SearchByNpCharge()

    # eq 30: Altria(自充30) + Altria Caster(群充30)
    results = execute_skill(skill, SERVANTS, {"op": "eq", "value": 30})
    assert names(results) == ["Altria Pendragon", "Altria Caster"]

    # gte 50
    results = execute_skill(skill, SERVANTS, {"op": "gte", "value": 50})
    assert names(results) == ["James Moriarty", "Altria Caster"]


def test_rarity_and_class_filters():
    """迁移自 test_rarity_class_and_nickname_filters 的前半部分。"""
    rarity_skill = SearchByRarity()
    class_skill = SearchByClass()

    # 五星 + 剑阶（AND 组合：两个 Skill 串联过滤）
    r1 = execute_skill(rarity_skill, SERVANTS, {"op": "eq", "value": 5})
    r2 = execute_skill(class_skill, r1, {"class_name": "saber"})
    assert names(r2) == ["Altria Pendragon"]


@patch("server.skills.query.lookup_servant.load_nicknames", return_value=NICKNAMES)
def test_nickname_lookup(mock_nick):
    """迁移自 test_rarity_class_and_nickname_filters 的昵称部分。"""
    skill = LookupServant()

    assert names(execute_skill(skill, SERVANTS, {"name": "呆毛"})) == ["Altria Pendragon"]
    assert names(execute_skill(skill, SERVANTS, {"name": "小教授"})) == ["James Moriarty"]
    assert names(execute_skill(skill, SERVANTS, {"name": "水 C 呆"})) == ["Altria Caster"]
    assert names(execute_skill(skill, SERVANTS, {"name": "泳装阿尔托莉雅"})) == ["Altria Caster"]


def test_single_effect_and_target_type_filters():
    """迁移自 test_single_effect_and_target_type_filters。"""
    skill = SearchBySkillEffect()

    # upAtk + party target
    results = execute_skill(skill, SERVANTS, {"effects": ["upAtk"], "target_type": "party"})
    assert names(results) == ["Altria Pendragon"]

    # upAtk + self target → 无匹配
    results = execute_skill(skill, SERVANTS, {"effects": ["upAtk"], "target_type": "self"})
    assert results == []


def test_skill_effects_and_or_filters():
    """迁移自 test_skill_effects_and_or_filters。"""
    skill = SearchBySkillEffect()

    # AND: avoidance + guts
    results = execute_skill(skill, SERVANTS, {"effects": ["avoidance", "guts"], "effects_op": "and"})
    assert names(results) == ["James Moriarty"]

    # OR: invincible 或 guts
    results = execute_skill(skill, SERVANTS, {"effects": ["invincible", "guts"], "effects_op": "or"})
    assert names(results) == ["Altria Pendragon", "James Moriarty"]


def test_traits_cards_np_card_and_np_target_filters():
    """迁移自 test_traits_cards_np_card_and_np_target_filters。"""
    traits_skill = SearchByTraits()
    cards_skill = SearchByCards()

    # traits: 秩序(300) + 善(303)，排除 1002
    results = execute_skill(traits_skill, SERVANTS, {"traits": [300, 303], "exclude_traits": [1002]})
    assert names(results) == ["Altria Pendragon"]

    # cards: 3蓝 + arts 宝具
    results = execute_skill(cards_skill, SERVANTS, {"cards": {"arts": 3}, "np_card": "arts"})
    assert names(results) == ["James Moriarty", "Altria Caster", "Hans Christian Andersen"]

    # npTarget: support
    results = execute_skill(cards_skill, SERVANTS, {"np_target": "support"})
    assert names(results) == ["Altria Caster", "Hans Christian Andersen"]


def test_np_effect_single_filter():
    """迁移自 test_np_effect_single_filter。"""
    skill = SearchByNpEffect()

    # upAtk 宝具效果
    results = execute_skill(skill, SERVANTS, {"effects": ["upAtk"]})
    assert names(results) == ["Altria Pendragon", "James Moriarty"]

    # gainNp 宝具效果只有 Altria Caster 有
    results = execute_skill(skill, SERVANTS, {"effects": ["gainNp"]})
    assert names(results) == ["Altria Caster"]

    # 无匹配
    results = execute_skill(skill, SERVANTS, {"effects": ["upCriticaldamage"]})
    assert results == []


def test_np_effects_and_or_filters():
    """迁移自 test_np_effects_and_or_filters。"""
    skill = SearchByNpEffect()

    # AND: invincible + upAtk → Moriarty
    results = execute_skill(skill, SERVANTS, {"effects": ["invincible", "upAtk"], "effects_op": "and"})
    assert names(results) == ["James Moriarty"]

    # OR: invincible 或 upArts → Moriarty + Caster
    results = execute_skill(skill, SERVANTS, {"effects": ["invincible", "upArts"], "effects_op": "or"})
    assert names(results) == ["James Moriarty", "Altria Caster"]


def test_attribute_filter():
    """SearchByAttribute 额外覆盖。"""
    skill = SearchByAttribute()

    results = execute_skill(skill, SERVANTS, {"gender": "female"})
    assert names(results) == ["Altria Pendragon", "Altria Caster"]

    results = execute_skill(skill, SERVANTS, {"attribute": "human"})
    assert names(results) == ["James Moriarty", "Hans Christian Andersen"]


@patch("server.skills.query.lookup_servant.load_nicknames", return_value=NICKNAMES)
def test_compare_servants(mock_nick):
    """CompareServants 覆盖。"""
    skill = CompareServants()

    results = skill.execute(SERVANTS, {"names": ["呆毛", "小教授"]})
    assert names(results) == ["Altria Pendragon", "James Moriarty"]

    # 单个名称也能工作
    results = skill.execute(SERVANTS, {"names": ["水C呆"]})
    assert names(results) == ["Altria Caster"]
