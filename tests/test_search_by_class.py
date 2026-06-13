"""search_by_class Skill 单元测试。

回归覆盖 hotfix v0.4.5：LLM 输出中文职阶名（「狂阶」「术阶」等）时，
search_by_class.filter 必须能反查回英文（berserker/caster）再与 servant DB 比较，
避免 0 命中。
"""

import server.skills  # noqa: F401 — 触发 @register_skill 注册
from server.skills.base import SKILL_REGISTRY


def _filter(class_name, servant_class):
    """Helper: 用 SearchByClass.filter 判定单个 servant 是否命中。"""
    skill = SKILL_REGISTRY["search_by_class"]
    servant = {"className": servant_class}
    return skill.filter(servant, {"class_name": class_name})


class TestSearchByClassZhEnCompat:
    """中文/英文职阶名输入兼容性。"""

    def test_chinese_class_name_matches_english_servant(self):
        """LLM 输出中文「狂阶」应能命中 className=berserker 的从者。"""
        assert _filter("狂阶", "berserker") is True

    def test_chinese_class_name_misses_other_class(self):
        """中文「狂阶」不应命中 saber 从者。"""
        assert _filter("狂阶", "saber") is False

    def test_english_class_name_still_matches(self):
        """英文 berserker 输入向后兼容,应命中 berserker 从者。"""
        assert _filter("berserker", "berserker") is True

    def test_english_class_name_case_insensitive(self):
        """英文输入大小写不敏感。"""
        assert _filter("Caster", "caster") is True
        assert _filter("CASTER", "caster") is True

    def test_chinese_with_whitespace(self):
        """中文输入两端带空格也应命中。"""
        assert _filter(" 术阶 ", "caster") is True

    def test_unknown_class_name_returns_false(self):
        """无法识别的职阶名应返回 False(0 命中,触发 fallback)。"""
        assert _filter("未知阶", "berserker") is False

    def test_none_class_name_returns_true(self):
        """class_name 为 None 时应放行(保持向后兼容)。"""
        skill = SKILL_REGISTRY["search_by_class"]
        servant = {"className": "saber"}
        assert skill.filter(servant, {"class_name": None}) is True

    def test_all_main_classes_zh_to_en(self):
        """覆盖 7 大主流职阶的中→英反查。"""
        cases = [
            ("剑阶", "saber"),
            ("弓阶", "archer"),
            ("枪阶", "lancer"),
            ("骑阶", "rider"),
            ("术阶", "caster"),
            ("杀阶", "assassin"),
            ("狂阶", "berserker"),
        ]
        for cn, en in cases:
            assert _filter(cn, en) is True, f"{cn} 应命中 {en}"


class TestDescribeFiltersClassName:
    """translation.describe_filters 对 search_by_class 的人类可读描述兼容性。"""

    def test_describe_with_camel_case_key(self):
        """routing 阶段产生的 className(camelCase) 应被正确显示。"""
        from server.translation import describe_filters

        descs = describe_filters([{"skill_name": "search_by_class", "params": {"className": "狂阶"}}])
        assert "职阶 = 狂阶" in descs

    def test_describe_with_snake_case_key(self):
        """execution 阶段经 Pydantic alias 转成 class_name(snake_case) 也应正确显示。"""
        from server.translation import describe_filters

        descs = describe_filters([{"skill_name": "search_by_class", "params": {"class_name": "狂阶"}}])
        assert "职阶 = 狂阶" in descs

    def test_describe_english_translates_to_chinese(self):
        """英文 className 输入时应反查显示为中文,提升可读性。"""
        from server.translation import describe_filters

        descs = describe_filters([{"skill_name": "search_by_class", "params": {"className": "berserker"}}])
        assert "职阶 = 狂阶" in descs
