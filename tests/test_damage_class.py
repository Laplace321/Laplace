"""damageClass 乘区标签烘焙测试。

严格遵守 AGENTS.md 伤害公式定义：
- A 类：色卡性能（upArts/upBuster/upQuick）
- B 类：攻击力（upAtk）
- C 类：特攻/暴击/宝具威力（upDamage/upCriticaldamage/upNpdamage）
- D 类：宝具特攻倍率（damageNpSP）
- 防御类 buff（upDefence/subSelfdamage）严禁标记 damageClass
"""

import json
from pathlib import Path

from server.data_loader import build_effect_matcher, extract_skill_effects, merge_effect_overlay


def _load_merged_schema() -> dict:
    """加载合并后的 effect schema（含 overlay）。"""
    schema_path = Path(__file__).parent.parent / "server" / "knowledge" / "effect_schema.json"
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)
    merged_effects = merge_effect_overlay(schema.get("effects", []))
    schema["effects"] = merged_effects
    return schema


def _make_matcher() -> dict:
    return build_effect_matcher(_load_merged_schema())


class TestDamageClassOverlay:
    """验证 effect_overrides.json 中 damageClass 标签正确性。"""

    def test_damage_classes_in_matcher(self):
        """build_effect_matcher 应正确提取 damageClasses 索引。"""
        matcher = _make_matcher()
        dc = matcher.get("damageClasses", {})

        assert dc.get("upArts") == "A"
        assert dc.get("upBuster") == "A"
        assert dc.get("upQuick") == "A"
        assert dc.get("upAtk") == "B"
        assert dc.get("upDamage") == "C"
        assert dc.get("upCriticaldamage") == "C"
        assert dc.get("upNpdamage") == "C"
        assert dc.get("damageNpSP") == "D"

    def test_no_damage_class_for_defence_buffs(self):
        """防御类 buff 严禁标记 damageClass。"""
        matcher = _make_matcher()
        dc = matcher.get("damageClasses", {})

        assert "upDefence" not in dc, "upDefence 不应有 damageClass（己方防御 buff，非增伤）"
        assert "subSelfdamage" not in dc, "subSelfdamage 不应有 damageClass（减伤 buff，非增伤）"

    def test_no_damage_class_for_non_damage_effects(self):
        """非伤害效果不应有 damageClass。"""
        matcher = _make_matcher()
        dc = matcher.get("damageClasses", {})

        non_damage = ["gainNp", "gainHp", "gainStar", "avoidance", "invincible", "guts", "addDamage"]
        for eff in non_damage:
            assert eff not in dc, f"{eff} 不应有 damageClass"


class TestDamageClassInSkillDetails:
    """验证 damageClass 正确烘焙到 skillDetails 效果条目。"""

    def test_skill_effect_has_damage_class(self):
        """upAtk 效果条目应有 damageClass=B。"""
        matcher = _make_matcher()
        svt = {
            "skills": [
                {
                    "id": 100,
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
        skill1 = details[0]
        atk_effs = [e for e in skill1["effects"] if e["type"] == "upAtk"]
        assert len(atk_effs) == 1
        assert atk_effs[0]["damageClass"] == "B"

    def test_defence_effect_no_damage_class(self):
        """upDefence 效果条目不应有 damageClass。"""
        matcher = _make_matcher()
        svt = {
            "skills": [
                {
                    "id": 200,
                    "type": "active",
                    "num": 1,
                    "name": "Defence Up",
                    "coolDown": [7],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "self",
                            "buffs": [{"type": "upDefence", "vals": [], "ckSelfIndv": [], "ckOpIndv": []}],
                            "svals": [{"Rate": 1000, "Value": 300, "Turn": 3, "Count": -1}],
                        },
                    ],
                }
            ],
        }

        _, details = extract_skill_effects(svt, matcher)
        skill1 = details[0]
        def_effs = [e for e in skill1["effects"] if e["type"] == "upDefence"]
        assert len(def_effs) == 1
        assert "damageClass" not in def_effs[0], "upDefence 不应有 damageClass"

    def test_gain_np_no_damage_class(self):
        """gainNp 效果条目不应有 damageClass。"""
        matcher = _make_matcher()
        svt = {
            "skills": [
                {
                    "id": 300,
                    "type": "active",
                    "num": 1,
                    "name": "NP Charge",
                    "coolDown": [6],
                    "functions": [
                        {
                            "funcType": "gainNp",
                            "funcTargetType": "self",
                            "buffs": [],
                            "svals": [{"Rate": 1000, "Value": 5000}],
                        },
                    ],
                }
            ],
        }

        _, details = extract_skill_effects(svt, matcher)
        skill1 = details[0]
        np_effs = [e for e in skill1["effects"] if e["type"] == "gainNp"]
        assert len(np_effs) == 1
        assert "damageClass" not in np_effs[0]
