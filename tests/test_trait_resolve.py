"""测试 resolve_trait_names 的别名匹配、子串匹配改进逻辑，以及 _merge_traits 特性合并。"""

import pytest

from server.data_loader import _merge_traits
from server.skills.query.search_by_traits import (
    _ensure_trait_name_map,
    _get_trait_aliases,
    resolve_trait_names,
)


@pytest.fixture(autouse=True)
def _ensure_maps_loaded():
    """确保 trait name map 已加载。"""
    _ensure_trait_name_map()


class TestAliasResolution:
    """别名匹配测试（config/trait_aliases.json）。"""

    def test_alias_file_loads(self):
        aliases = _get_trait_aliases()
        assert isinstance(aliases, dict)
        assert "灵衣特性从者" in aliases

    def test_alias_resolves_to_correct_id(self):
        """灵衣特性从者 → 灵衣持有者(ID=2780)"""
        ids = resolve_trait_names(["灵衣特性从者"])
        assert 2780 in ids

    def test_alias_takes_priority_over_substring(self):
        """别名匹配应优先于子串匹配，不应匹配到从者(1000)"""
        ids = resolve_trait_names(["灵衣特性从者"])
        assert 1000 not in ids

    def test_multiple_aliases(self):
        """多个别名都能正确解析"""
        ids = resolve_trait_names(["灵衣特性", "灵衣从者"])
        assert ids == [2780, 2780]


class TestShortNameProtection:
    """短特性名保护：len(cn) < 3 的特性不参与 cn in name 方向子串匹配。"""

    def test_short_trait_not_reverse_match_long_query(self):
        """从者(len=2) 不应反向命中"灵衣特性从者"这样的长查询"""
        # 如果别名表没有覆盖，子串匹配也不应命中"从者"
        # 用一个不在别名表中的假名测试
        ids = resolve_trait_names(["某某从者特性测试"])
        # 不应返回 1000(从者)
        assert 1000 not in ids

    def test_exact_short_name_still_works(self):
        """精确输入"龙"仍可正确匹配（精确匹配路径）"""
        name_map = _ensure_trait_name_map()
        if "龙" in name_map:
            ids = resolve_trait_names(["龙"])
            assert name_map["龙"] in ids

    def test_exact_short_name_cong_zhe(self):
        """精确输入"从者"仍可匹配（精确匹配路径，非子串）"""
        name_map = _ensure_trait_name_map()
        if "从者" in name_map:
            ids = resolve_trait_names(["从者"])
            assert 1000 in ids


class TestLongestSubstringMatch:
    """最长子串优先策略。"""

    def test_longest_match_preferred(self):
        """兽科从者(len=4) 应精确匹配而非子串匹配到更短的候选"""
        ids = resolve_trait_names(["兽科从者"])
        assert 2821 in ids
        assert 1000 not in ids  # 不应命中"从者"

    def test_combined_query_correct(self):
        """复现 trace 830e5ebb：兽科从者 + 灵衣特性从者 应为 [2821, 2780]"""
        ids = resolve_trait_names(["兽科从者", "灵衣特性从者"])
        assert 2821 in ids
        assert 2780 in ids
        assert 1000 not in ids
        assert len(ids) == 2


class TestExistingBehaviorPreserved:
    """确保现有行为不被破坏。"""

    def test_alignment_combo(self):
        """阵营组合解析仍正常"""
        ids = resolve_trait_names(["秩序善"])
        assert 300 in ids  # 秩序
        assert 303 in ids  # 善

    def test_numeric_passthrough(self):
        """数字直传仍正常"""
        ids = resolve_trait_names(["2821"])
        assert 2821 in ids

    def test_empty_input(self):
        """空输入返回空列表"""
        assert resolve_trait_names([]) == []
        assert resolve_trait_names([""]) == []


class TestMergeTraitsCostume:
    """测试 _merge_traits 对 costume individuality 的合并逻辑。"""

    def test_costume_traits_merged_into_union(self):
        """灵衣阶段携带的特性应被合并到 traits 并集中。"""
        svt = {
            "traits": [{"id": 1000}, {"id": 2001}],  # 基础: 从者, humanoid
            "ascensionAdd": {
                "individuality": {
                    "ascension": {},
                    "costume": {
                        "301330": [
                            {"id": 1000},
                            {"id": 2001},
                            {"id": 2780},  # 灵衣持有者
                            {"id": 2821},  # 兽科从者
                        ]
                    },
                }
            },
            "traitAdd": [],
        }
        result = _merge_traits(svt)
        assert 2821 in result["traits"], "灵衣特性 2821(兽科从者) 应在 traits 并集中"
        assert 2780 in result["traits"], "灵衣特性 2780(灵衣持有者) 应在 traits 并集中"
        assert 1000 in result["traits"]

    def test_costume_creates_traits_by_ascension(self):
        """灵衣与基础灵基有差异时，应生成 traitsByAscension。"""
        svt = {
            "traits": [{"id": 1000}, {"id": 2001}],
            "ascensionAdd": {
                "individuality": {
                    "ascension": {},
                    "costume": {
                        "102830": [
                            {"id": 1000},
                            {"id": 2001},
                            {"id": 2821},  # 灵衣独有
                        ]
                    },
                }
            },
            "traitAdd": [],
        }
        result = _merge_traits(svt)
        # 灵衣阶段有额外特性，应产生灵基差异
        assert "traitsByAscension" in result
        assert "102830" in result["traitsByAscension"]
        assert 2821 in result["traitsByAscension"]["102830"]

    def test_no_costume_section_no_change(self):
        """无 costume section 时行为不变。"""
        svt = {
            "traits": [{"id": 1000}, {"id": 2821}],
            "ascensionAdd": {"individuality": {"ascension": {}, "costume": {}}},
            "traitAdd": [],
        }
        result = _merge_traits(svt)
        assert 1000 in result["traits"]
        assert 2821 in result["traits"]

    def test_multiple_costumes_all_merged(self):
        """多件灵衣的特性都应被合并。"""
        svt = {
            "traits": [{"id": 1000}],
            "ascensionAdd": {
                "individuality": {
                    "ascension": {},
                    "costume": {
                        "102830": [{"id": 1000}, {"id": 2821}],
                        "102840": [{"id": 1000}, {"id": 2821}, {"id": 9999}],
                    },
                }
            },
            "traitAdd": [],
        }
        result = _merge_traits(svt)
        assert 2821 in result["traits"]
        assert 9999 in result["traits"]
