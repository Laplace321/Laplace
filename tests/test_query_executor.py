import server.query_executor as qe

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
        "totalSelfCharge": 30,
        "hasNpCharge": True,
        "skillEffects": ["gainNp", "invincible", "upAtk"],
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
        "npCharges": [{"chargePercent": 50, "targetType": "party"}],
        "totalSelfCharge": 50,
        "hasNpCharge": True,
        "skillEffects": ["gainNp", "avoidance", "guts"],
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
        "totalSelfCharge": 0,
        "hasNpCharge": False,
        "skillEffects": ["upCriticaldamage"],
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
        "npCharges": [{"chargePercent": 50, "targetType": "party"}],
        "totalSelfCharge": 50,
        "hasNpCharge": True,
        "skillEffects": ["gainNp", "upArts"],
        "skillDetails": [],
        "traits": [308],
        "gender": "female",
        "attribute": "star",
        "cards": {"buster": 1, "arts": 3, "quick": 1},
        "npCard": "arts",
        "npTarget": "support",
    },
]


def setup_module():
    qe._servants_db = SERVANTS
    qe._nicknames = {
        "呆毛": {"name": "阿尔托莉雅·潘德拉贡", "className": "saber"},
        "小教授": {"name": "詹姆斯·莫里亚蒂", "className": "ruler"},
        "水C呆": {"name": "阿尔托莉雅·卡斯特", "className": "berserker"},
        "泳装阿尔托莉雅": {"name": "阿尔托莉雅·卡斯特", "className": "berserker"},
    }


def teardown_module():
    qe._servants_db = None
    qe._nicknames = None


def names(results):
    return [s["name"] for s in results]


def test_np_charge_exact_and_gte_filters():
    assert names(qe.execute_query({"npCharge": {"op": "eq", "value": 30}})) == ["Altria Pendragon"]
    assert names(qe.execute_query({"npCharge": {"op": "gte", "value": 50}})) == [
        "James Moriarty",
        "Altria Caster",
    ]


def test_rarity_class_and_nickname_filters():
    assert names(qe.execute_query({"rarity": {"op": "eq", "value": 5}, "className": "saber"})) == ["Altria Pendragon"]
    assert names(qe.execute_query({"name": "呆毛"})) == ["Altria Pendragon"]
    assert names(qe.execute_query({"name": "小教授"})) == ["James Moriarty"]
    assert names(qe.execute_query({"name": "水 C 呆"})) == ["Altria Caster"]
    assert names(qe.execute_query({"name": "泳装阿尔托莉雅"})) == ["Altria Caster"]


def test_single_effect_and_target_type_filters():
    assert names(qe.execute_query({"skillEffect": "upAtk", "targetType": "party"})) == ["Altria Pendragon"]
    assert qe.execute_query({"skillEffect": "upAtk", "targetType": "self"}) == []


def test_skill_effects_and_or_filters():
    assert names(qe.execute_query({"skillEffects": ["avoidance", "guts"]})) == ["James Moriarty"]
    assert names(qe.execute_query({"skillEffects": ["invincible", "guts"], "skillEffectsOp": "or"})) == [
        "Altria Pendragon",
        "James Moriarty",
    ]


def test_traits_cards_np_card_and_np_target_filters():
    assert names(qe.execute_query({"traits": [300, 303], "excludeTraits": [1002]})) == ["Altria Pendragon"]
    assert names(qe.execute_query({"cards": {"arts": 3}, "npCard": "arts"})) == [
        "James Moriarty",
        "Altria Caster",
        "Hans Christian Andersen",
    ]
    assert names(qe.execute_query({"npTarget": "support"})) == [
        "Altria Caster",
        "Hans Christian Andersen",
    ]
