"""antiTraitIndex 特攻索引构建测试。

验证从 skillDetails（C类特攻 upDamage + ckOpIndv）和
npDetails（D类宝具特攻 damageNpSP + vals）中正确提取特攻目标，
构建从者顶层 antiTraitIndex 索引。
"""

import json
from pathlib import Path

from server.data_loader import build_effect_matcher, extract_skill_effects, merge_effect_overlay


def _load_merged_schema() -> dict:
    schema_path = Path(__file__).parent.parent / "server" / "knowledge" / "effect_schema.json"
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    merged_effects = merge_effect_overlay(schema.get("effects", []))
    schema["effects"] = merged_effects
    return schema


def _make_matcher() -> dict:
    return build_effect_matcher(_load_merged_schema())


class TestAntiTargetExtraction:
    """验证 extract_skill_effects 中 antiTarget 提取。"""

    def test_up_damage_with_ck_op_indv(self):
        """upDamage + ckOpIndv 应提取 antiTarget（如齐格飞对龙特攻）。"""
        matcher = _make_matcher()
        svt = {
            "skills": [
                {
                    "id": 100,
                    "type": "active",
                    "num": 3,
                    "name": "Golden Rule",
                    "coolDown": [7],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "self",
                            "buffs": [
                                {
                                    "type": "upDamage",
                                    "vals": [3006],
                                    "ckSelfIndv": [],
                                    "ckOpIndv": [{"id": 301, "name": "dragon"}],
                                }
                            ],
                            "svals": [{"Rate": 1000, "Value": 800, "Turn": 3, "Count": -1}],
                        },
                    ],
                }
            ],
        }

        _, details = extract_skill_effects(svt, matcher)
        skill3 = details[0]
        damage_effs = [e for e in skill3["effects"] if e["type"] == "upDamage"]
        assert len(damage_effs) == 1

        anti = damage_effs[0].get("antiTarget")
        assert anti is not None, "upDamage + ckOpIndv 应产生 antiTarget"
        assert anti["trait"] == "dragon"
        assert anti["traitId"] == 301
        assert damage_effs[0]["damageClass"] == "C"

    def test_up_damage_without_ck_op_indv(self):
        """upDamage 无 ckOpIndv 时不应有 antiTarget（如通用特攻状态）。"""
        matcher = _make_matcher()
        svt = {
            "skills": [
                {
                    "id": 200,
                    "type": "active",
                    "num": 1,
                    "name": "Generic Damage Up",
                    "coolDown": [7],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "self",
                            "buffs": [
                                {
                                    "type": "upDamage",
                                    "vals": [3006],
                                    "ckSelfIndv": [],
                                    "ckOpIndv": [],
                                }
                            ],
                            "svals": [{"Rate": 1000, "Value": 500, "Turn": 3, "Count": -1}],
                        },
                    ],
                }
            ],
        }

        _, details = extract_skill_effects(svt, matcher)
        damage_effs = [e for e in details[0]["effects"] if e["type"] == "upDamage"]
        assert len(damage_effs) == 1
        assert "antiTarget" not in damage_effs[0]

    def test_non_damage_effect_no_anti_target(self):
        """非 upDamage 效果不应有 antiTarget。"""
        matcher = _make_matcher()
        svt = {
            "skills": [
                {
                    "id": 300,
                    "type": "active",
                    "num": 1,
                    "name": "Charisma",
                    "coolDown": [7],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "ptAll",
                            "buffs": [{"type": "upAtk", "vals": [3006], "ckSelfIndv": [], "ckOpIndv": []}],
                            "svals": [{"Rate": 1000, "Value": 200, "Turn": 3, "Count": -1}],
                        },
                    ],
                }
            ],
        }

        _, details = extract_skill_effects(svt, matcher)
        atk_effs = [e for e in details[0]["effects"] if e["type"] == "upAtk"]
        assert len(atk_effs) == 1
        assert "antiTarget" not in atk_effs[0]


class TestAntiTraitIndexBuilding:
    """验证 antiTraitIndex 顶层索引构建逻辑（在 build_database 中）。

    由于 build_database 需要网络请求，这里用单元逻辑验证索引构建规则。
    """

    def test_index_from_skill_details(self):
        """从 skillDetails 中提取 antiTarget 构建索引。"""
        skill_details = [
            {
                "skillNum": 3,
                "effects": [
                    {
                        "type": "upDamage",
                        "damageClass": "C",
                        "antiTarget": {"trait": "dragon", "traitId": 301},
                    },
                    {"type": "upAtk", "damageClass": "B"},
                ],
            }
        ]
        anti_trait_index = []
        for sk in skill_details:
            for eff in sk.get("effects", []):
                anti_target = eff.get("antiTarget")
                if anti_target:
                    anti_trait_index.append(
                        {
                            "trait": anti_target["trait"],
                            "traitId": anti_target["traitId"],
                            "source": "skill",
                            "buffClass": eff.get("damageClass", "C"),
                            "effectType": eff["type"],
                        }
                    )

        assert len(anti_trait_index) == 1
        assert anti_trait_index[0]["trait"] == "dragon"
        assert anti_trait_index[0]["traitId"] == 301
        assert anti_trait_index[0]["source"] == "skill"
        assert anti_trait_index[0]["buffClass"] == "C"

    def test_index_from_np_details(self):
        """从 npDetails 中提取宝具特攻目标构建索引。"""
        np_details = [
            {
                "npId": 100,
                "effects": [
                    {
                        "type": "damageNpSP",
                        "damageClass": "D",
                        "antiTarget": {"trait": "female", "traitId": 102},
                        "correction": 1500,
                    },
                ],
            }
        ]

        anti_trait_index = []
        for np_d in np_details:
            for eff in np_d.get("effects", []):
                anti_target = eff.get("antiTarget")
                if anti_target:
                    anti_trait_index.append(
                        {
                            "trait": anti_target["trait"],
                            "traitId": anti_target["traitId"],
                            "source": "np",
                            "buffClass": eff.get("damageClass", "D"),
                            "effectType": eff["type"],
                        }
                    )

        assert len(anti_trait_index) == 1
        assert anti_trait_index[0]["trait"] == "female"
        assert anti_trait_index[0]["source"] == "np"
        assert anti_trait_index[0]["buffClass"] == "D"

    def test_empty_for_no_special(self):
        """无特攻效果时 antiTraitIndex 应为空。"""
        skill_details = [{"skillNum": 1, "effects": [{"type": "upAtk", "damageClass": "B"}]}]
        np_details = [{"npId": 100, "effects": [{"type": "upNpdamage", "damageClass": "C"}]}]

        anti_trait_index = []
        for sk in skill_details:
            for eff in sk.get("effects", []):
                if eff.get("antiTarget"):
                    anti_trait_index.append(eff["antiTarget"])
        for np_d in np_details:
            for eff in np_d.get("effects", []):
                if eff.get("antiTarget"):
                    anti_trait_index.append(eff["antiTarget"])

        assert len(anti_trait_index) == 0
