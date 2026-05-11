"""
Tool Handlers 单元测试。

测试范围：
- handle_search_servants 参数映射
- handle_lookup_servant / handle_compare_servants
- handle_list_effects / handle_list_traits / handle_list_classes（本地查表）
- _build_servant_detail MV 字段映射

所有测试直接调用 handler 函数，不经过 LLM，纯确定性测试。
"""

import server.skills  # noqa: F401 — 触发 @register_skill 注册
from server.agent.tool_handlers import (
    _build_servant_detail,
    handle_list_classes,
    handle_list_effects,
    handle_list_traits,
    handle_lookup_servant,
    handle_search_servants,
)

# ============================================================
# 查表 handlers 测试
# ============================================================


class TestListEffects:
    """测试 handle_list_effects。"""

    def test_returns_effects_list(self):
        result = handle_list_effects({})
        assert "total" in result
        assert "effects" in result
        assert result["total"] > 0
        # 每个效果应有 name 和 aliases_zh
        for eff in result["effects"]:
            assert "name" in eff
            assert "aliases_zh" in eff

    def test_composite_effects_have_includes(self):
        """复合效果（如 damageBoost）应包含 composite 和 includes 字段。"""
        result = handle_list_effects({})
        composites = [e for e in result["effects"] if e.get("composite")]
        assert len(composites) > 0, "应至少有一个复合效果（如 damageBoost）"
        for c in composites:
            assert "includes" in c
            assert len(c["includes"]) > 0


class TestListTraits:
    """测试 handle_list_traits。"""

    def test_returns_traits_list(self):
        result = handle_list_traits({})
        assert "total" in result
        assert "traits" in result
        assert result["total"] > 0
        for t in result["traits"]:
            assert "id" in t
            assert "name_cn" in t


class TestListClasses:
    """测试 handle_list_classes — 从 config/translations.json 加载。"""

    def test_returns_class_list(self):
        result = handle_list_classes({})
        assert "total" in result
        assert "classes" in result
        assert result["total"] > 0
        # 应包含常见职阶
        keys = {c["key"] for c in result["classes"]}
        assert "saber" in keys or "Saber" in keys  # translations.json 用小写
        assert "caster" in keys or "Caster" in keys

    def test_each_class_has_key_and_name(self):
        result = handle_list_classes({})
        for c in result["classes"]:
            assert "key" in c
            assert "name_cn" in c
            assert len(c["name_cn"]) > 0


# ============================================================
# 核心查询 handlers 测试
# ============================================================


class TestSearchServants:
    """测试 handle_search_servants 参数映射。"""

    def test_empty_params_returns_hint(self):
        """无任何参数时返回提示。"""
        result = handle_search_servants({})
        assert result["total"] == 0
        assert "message" in result

    def test_class_filter(self):
        """按职阶筛选。"""
        result = handle_search_servants({"class_name": "Saber"})
        assert result["total"] > 0
        # 结果中所有从者应为 Saber
        for s in result["top_results"]:
            assert s["class"].lower() == "saber"

    def test_rarity_filter(self):
        """按稀有度筛选。"""
        result = handle_search_servants({"rarity": 5})
        assert result["total"] > 0
        for s in result["top_results"]:
            assert s["rarity"] == 5

    def test_combined_filters(self):
        """组合筛选：5星 Caster。"""
        result = handle_search_servants({"class_name": "Caster", "rarity": 5})
        assert result["total"] > 0
        for s in result["top_results"]:
            assert s["class"].lower() == "caster"
            assert s["rarity"] == 5

    def test_effect_filter(self):
        """按效果筛选。"""
        result = handle_search_servants({"effects": ["gainNp"]})
        assert result["total"] > 0

    def test_np_card_filter(self):
        """按宝具卡色筛选。"""
        result = handle_search_servants({"np_card": "arts"})
        assert result["total"] > 0

    def test_attribute_chinese_mapping(self):
        """中文属性应被正确映射为英文。"""
        result_zh = handle_search_servants({"attribute": "天", "rarity": 5})
        result_en = handle_search_servants({"attribute": "sky", "rarity": 5})
        assert result_zh["total"] == result_en["total"]
        assert result_zh["total"] > 0


class TestLookupServant:
    """测试 handle_lookup_servant。"""

    def test_lookup_by_en_name(self):
        """按英文名查询（模糊匹配，可能命中多个同名变体）。"""
        result = handle_lookup_servant({"name": "Altria Pendragon"})
        assert "error" not in result
        assert "id" in result
        assert "Altria Pendragon" in result["name_en"]

    def test_lookup_not_found(self):
        """查询不存在的从者应返回 error。"""
        result = handle_lookup_servant({"name": "不存在的从者12345"})
        assert "error" in result

    def test_lookup_empty_name(self):
        """空名称应返回 error。"""
        result = handle_lookup_servant({"name": ""})
        assert "error" in result


# ============================================================
# _build_servant_detail 字段映射测试
# ============================================================


class TestBuildServantDetail:
    """测试 _build_servant_detail 对 MV 字段的正确映射。"""

    def test_basic_fields(self):
        """基本字段映射。"""
        mock_servant = {
            "collectionNo": 1,
            "name": "Mash Kyrielight",
            "aliasCN": "玛修·基列莱特",
            "className": "shielder",
            "rarity": 4,
            "hpMax": 10000,
            "atkMax": 7000,
            "npCard": "arts",
            "npTarget": "support",
            "skillDetails": [
                {
                    "skillName": "Obscurant Wall of Chalk",
                    "skillNum": 1,
                    "effects": [{"name": "invincible", "target": "ptOne"}],
                }
            ],
            "npDetails": [
                {
                    "npName": "Lord Camelot",
                    "effects": [{"name": "defenceUp", "target": "ptAll"}],
                }
            ],
        }
        detail = _build_servant_detail(mock_servant)

        assert detail["id"] == 1
        assert detail["name"] == "玛修·基列莱特"
        assert detail["name_en"] == "Mash Kyrielight"
        assert detail["class"] == "shielder"
        assert detail["rarity"] == 4
        assert detail["hp_max"] == 10000
        assert detail["atk_max"] == 7000

    def test_skill_details_field_names(self):
        """skillDetails 使用 skillName 而非 name。"""
        mock_servant = {
            "collectionNo": 1,
            "name": "Test",
            "className": "saber",
            "rarity": 5,
            "skillDetails": [
                {
                    "skillName": "Charisma B",
                    "skillNum": 1,
                    "effects": [{"name": "upAtk", "target": "ptAll"}],
                }
            ],
            "npDetails": [],
        }
        detail = _build_servant_detail(mock_servant)
        assert detail["skills"][0]["name"] == "Charisma B"
        assert detail["skills"][0]["skill_num"] == 1

    def test_np_details_field_names(self):
        """npDetails 使用 npName，卡色和目标从顶级字段读取。"""
        mock_servant = {
            "collectionNo": 2,
            "name": "Artoria",
            "className": "saber",
            "rarity": 5,
            "npCard": "buster",
            "npTarget": "all",
            "skillDetails": [],
            "npDetails": [
                {
                    "npName": "Excalibur",
                    "effects": [{"name": "damageNp", "target": "enemyAll"}],
                }
            ],
        }
        detail = _build_servant_detail(mock_servant)
        np = detail["noble_phantasm"][0]
        assert np["name"] == "Excalibur"
        assert np["card"] == "buster"
        assert np["target"] == "all"
