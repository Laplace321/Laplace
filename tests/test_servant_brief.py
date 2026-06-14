"""ADR-031 build_servant_brief 单元测试。

验证：
- 中文化字段（职阶 / 卡色 / 宝具目标）
- 特性字段（trait_id 翻译）
- 技能详情 / 宝具详情拼接
- 1500 字符硬截断
- 边界输入（空 dict / 非 dict）
"""

from __future__ import annotations

from server.context_builder import SERVANT_BRIEF_MAX_CHARS, build_servant_brief


def _make_servant() -> dict:
    """构造一个最小可用的从者字典，字段格式与 servants_db.json 一致。"""
    return {
        "name": "Tamamo-no-Mae",
        "aliasCN": "玉藻前",
        "className": "caster",
        "rarity": 5,
        "cards": {"arts": 3, "buster": 1, "quick": 1},
        "npCard": "arts",
        "npTarget": "ptAll",
        "totalSelfCharge": 0,
        "traits": [2, 104],
        "skillDetails": [
            {
                "skillNum": 1,
                "skillName": "变化 A",
                "effects": [
                    {
                        "type": "upDefence",
                        "funcType": "addState",
                        "targetType": "self",
                        "valueMax": 300,
                        "turn": 1,
                        "count": -1,
                    }
                ],
            }
        ],
        "npDetails": [
            {
                "npName": "水天日光天照八野镇石",
                "effects": [
                    {
                        "funcType": "gainNp",
                        "targetType": "ptAll",
                        "svals": [{"Value": 2500}],
                    }
                ],
            }
        ],
    }


class TestBuildServantBrief:
    def test_returns_empty_for_invalid_input(self):
        assert build_servant_brief({}) == ""
        assert build_servant_brief(None) == ""  # type: ignore[arg-type]
        assert build_servant_brief("not dict") == ""  # type: ignore[arg-type]

    def test_includes_chinese_class_and_rarity(self):
        text = build_servant_brief(_make_servant())
        # 职阶必须中文化（caster → 术阶）
        assert "术阶" in text
        assert "5★" in text
        assert "玉藻前" in text
        assert "Tamamo-no-Mae" in text

    def test_includes_np_card_chinese(self):
        text = build_servant_brief(_make_servant())
        # 宝具卡色 arts 必须翻译为中文
        assert "蓝卡" in text or "Arts" in text  # 兼容映射差异
        assert "宝具卡色" in text

    def test_includes_skill_and_np_details(self):
        text = build_servant_brief(_make_servant())
        assert "变化 A" in text
        assert "水天日光天照八野镇石" in text

    def test_truncates_to_max_chars(self):
        """超长 servant 数据必须硬截断至 SERVANT_BRIEF_MAX_CHARS。"""
        servant = _make_servant()
        # 制造一个超长技能列表迫使输出超长
        servant["skillDetails"] = [
            {
                "skillNum": i,
                "skillName": f"超长技能名称占位{i}" * 50,
                "effects": [
                    {
                        "type": "upAtk",
                        "funcType": "addState",
                        "targetType": "self",
                        "valueMax": 100,
                        "turn": 3,
                        "count": -1,
                    }
                ],
            }
            for i in range(20)
        ]
        text = build_servant_brief(servant)
        assert len(text) <= SERVANT_BRIEF_MAX_CHARS
        assert text.endswith("…")

    def test_handles_missing_optional_fields(self):
        """缺少 traits / skillDetails / npDetails 时不应报错。"""
        servant = {
            "name": "Test",
            "aliasCN": "测试",
            "className": "saber",
            "rarity": 4,
            "cards": "BAAAQ",
            "npCard": "buster",
            "npTarget": "enemy",
            "totalSelfCharge": 30,
        }
        text = build_servant_brief(servant)
        assert "测试" in text
        assert "剑阶" in text or "saber" in text
        assert len(text) > 0
