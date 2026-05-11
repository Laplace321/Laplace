"""职阶克制关系查询测试。

覆盖：
1. class_relation.json 数据完整性与一致性
2. 中文职阶名 → 英文 className 反向映射
3. SearchByClassAdvantage.filter() 筛选逻辑
"""

import json
from pathlib import Path

import pytest

KNOWLEDGE_DIR = Path(__file__).parent.parent / "server" / "knowledge"
CONFIG_DIR = Path(__file__).parent.parent / "server" / "config"


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── 数据完整性 ──


PLAYABLE_CLASSES = [
    "saber",
    "archer",
    "lancer",
    "rider",
    "caster",
    "assassin",
    "berserker",
    "ruler",
    "alterEgo",
    "avenger",
    "moonCancer",
    "foreigner",
    "pretender",
]


class TestClassRelationData:
    """class_relation.json 数据完整性测试。"""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.data = _load_json(KNOWLEDGE_DIR / "class_relation.json")

    def test_has_advantage_and_reverse(self):
        assert "advantage" in self.data
        assert "reverse" in self.data

    def test_all_playable_classes_in_advantage(self):
        """13 个可玩职阶（除 shielder）都应有 advantage 条目。"""
        missing = [c for c in PLAYABLE_CLASSES if c not in self.data["advantage"]]
        assert not missing, f"advantage 缺少: {missing}"

    def test_all_playable_classes_in_reverse(self):
        """13 个可玩职阶（除 shielder）都应有 reverse 条目。"""
        missing = [c for c in PLAYABLE_CLASSES if c not in self.data["reverse"]]
        assert not missing, f"reverse 缺少: {missing}"

    def test_advantage_reverse_consistency(self):
        """advantage 和 reverse 必须互相一致。"""
        adv = self.data["advantage"]
        rev = self.data["reverse"]
        errors = []
        for atk, targets in adv.items():
            for t in targets:
                if atk not in rev.get(t, []):
                    errors.append(f"advantage[{atk}]->{t}, but reverse[{t}] missing {atk}")
        for dfn, attackers in rev.items():
            for a in attackers:
                if dfn not in adv.get(a, []):
                    errors.append(f"reverse[{dfn}]<-{a}, but advantage[{a}] missing {dfn}")
        assert not errors, "一致性错误:\n" + "\n".join(errors)

    def test_known_relations(self):
        """验证已知的克制关系。"""
        adv = self.data["advantage"]
        # 基础三骑三兵
        assert "lancer" in adv["saber"]
        assert "saber" in adv["archer"]
        assert "archer" in adv["lancer"]
        assert "caster" in adv["rider"]
        assert "assassin" in adv["caster"]
        assert "rider" in adv["assassin"]
        # Extra 职阶
        assert "foreigner" in adv["alterEgo"]
        assert "foreigner" in adv["foreigner"]  # foreigner 克制自身
        assert "pretender" in adv["foreigner"]
        assert "alterEgo" in adv["pretender"]
        # pretender 克制上三骑
        assert "saber" in adv["pretender"]
        assert "archer" in adv["pretender"]
        assert "lancer" in adv["pretender"]

    def test_known_reverse_relations(self):
        """验证 reverse 表的已知克制关系。"""
        rev = self.data["reverse"]
        # 克制 pretender 的职阶
        assert "foreigner" in rev["pretender"]
        # 克制 foreigner 的职阶
        assert "alterEgo" in rev["foreigner"]
        assert "foreigner" in rev["foreigner"]  # foreigner 自克


# ── 中文名反向映射 ──


class TestClassNameResolve:
    """中文职阶名 → 英文 className 反向映射测试。"""

    def test_resolve_chinese_names(self):
        from server.skills.query.search_by_class_advantage import resolve_class_name

        assert resolve_class_name("伪装者") == "pretender"
        assert resolve_class_name("骑阶") == "rider"
        assert resolve_class_name("术阶") == "caster"
        assert resolve_class_name("剑阶") == "saber"
        assert resolve_class_name("杀阶") == "assassin"
        assert resolve_class_name("狂阶") == "berserker"

    def test_resolve_english_names(self):
        from server.skills.query.search_by_class_advantage import resolve_class_name

        assert resolve_class_name("pretender") == "pretender"
        assert resolve_class_name("Pretender") == "pretender"
        assert resolve_class_name("saber") == "saber"

    def test_resolve_full_chinese_with_paren(self):
        from server.skills.query.search_by_class_advantage import resolve_class_name

        assert resolve_class_name("伪装者(Pretender)") == "pretender"
        assert resolve_class_name("他人格(AlterEgo)") == "alterego"

    def test_resolve_unknown_returns_none(self):
        from server.skills.query.search_by_class_advantage import resolve_class_name

        assert resolve_class_name("不存在的职阶") is None


# ── Skill filter 逻辑 ──


class TestSearchByClassAdvantageFilter:
    """SearchByClassAdvantage.filter() 筛选逻辑测试。"""

    @pytest.fixture(autouse=True)
    def _setup(self):
        # 清除缓存以确保测试隔离
        import server.skills.query.search_by_class_advantage as mod

        mod._CLASS_RELATION = None
        mod._CN_TO_CLASS.clear()

    def test_filter_advantage_pretender(self):
        """克制 pretender → 返回 foreigner（默认排除 berserker）。"""
        from server.skills.query.search_by_class_advantage import SearchByClassAdvantage

        skill = SearchByClassAdvantage()
        params = {"target_class": "伪装者", "include_berserker": False}

        assert skill.filter({"className": "foreigner"}, params) is True
        assert skill.filter({"className": "Foreigner"}, params) is True
        assert skill.filter({"className": "saber"}, params) is False
        assert skill.filter({"className": "berserker"}, params) is False

    def test_filter_advantage_pretender_with_berserker(self):
        """includeBerserker=True 时包含 berserker。"""
        from server.skills.query.search_by_class_advantage import SearchByClassAdvantage

        skill = SearchByClassAdvantage()
        params = {"target_class": "伪装者", "include_berserker": True}

        assert skill.filter({"className": "foreigner"}, params) is True
        assert skill.filter({"className": "berserker"}, params) is True

    def test_filter_advantage_caster(self):
        """克制 caster → 返回 rider 和 alterEgo（默认排除 berserker）。"""
        from server.skills.query.search_by_class_advantage import SearchByClassAdvantage

        skill = SearchByClassAdvantage()
        params = {"target_class": "术阶", "include_berserker": False}

        assert skill.filter({"className": "rider"}, params) is True
        assert skill.filter({"className": "alterEgo"}, params) is True
        assert skill.filter({"className": "berserker"}, params) is False
        assert skill.filter({"className": "saber"}, params) is False

    def test_filter_empty_target_passes_all(self):
        """target_class 为空时不过滤。"""
        from server.skills.query.search_by_class_advantage import SearchByClassAdvantage

        skill = SearchByClassAdvantage()
        assert skill.filter({"className": "saber"}, {"target_class": ""}) is True

    def test_get_advantage_classes(self):
        """直接测试 get_advantage_classes 函数。"""
        from server.skills.query.search_by_class_advantage import get_advantage_classes

        # 克制 pretender 的职阶（不含 berserker）
        result = get_advantage_classes("pretender", include_berserker=False)
        assert "foreigner" in result
        assert "berserker" not in result

        # 克制 pretender 的职阶（含 berserker）
        result = get_advantage_classes("pretender", include_berserker=True)
        assert "foreigner" in result
        assert "berserker" in result
