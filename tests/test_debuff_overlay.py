"""敌方 debuff effect overlay 完整性回归测试。

历史背景（trace 排查）：chaldea SkillEffect 仅暴露 55 个 up* 友好分类，
down* debuff 几乎全部缺失。未补齐前，伊斯坎达尔「王之军势」减防/减暴击率
function 在 _match_func_effects 返回空集后被静默丢弃。
"""

from server.data_loader import (
    _match_func_effects,
    build_effect_matcher,
    load_effect_schema,
)


def _make_func(func_type: str, buff_type: str = "", ck_self_indv=None) -> dict:
    func: dict = {"funcType": func_type, "svals": [{"Value": 100, "Turn": 3}]}
    if buff_type:
        buff: dict = {"type": buff_type, "ckSelfIndv": ck_self_indv or [], "ckOpIndv": []}
        func["buffs"] = [buff]
    else:
        func["buffs"] = []
    return func


def _matcher():
    return build_effect_matcher(load_effect_schema())


REQUIRED_DEBUFFS = [
    "downAtk",
    "downDefence",
    "downCriticalrate",
    "downCriticaldamage",
    "downCriticalpoint",
    "downStarweight",
    "downNpdamage",
    "downDropnp",
    "downDamage",
    "downSpecialdefence",
    "downGainHp",
    "downMaxhp",
    "downTolerance",
    "downToleranceSubstate",
    "downGrantstate",
    "downGrantInstantdeath",
    "downArts",
    "downBuster",
    "downQuick",
]


def test_schema_contains_all_core_debuffs():
    """合并 overlay 后 schema 必须包含全部核心 down* effect。"""
    schema = load_effect_schema()
    names = {e["name"] for e in schema.get("effects", [])}
    missing = [n for n in REQUIRED_DEBUFFS if n not in names]
    assert not missing, f"effect_schema 缺失核心 debuff: {missing}"


def test_matcher_buff_index_covers_core_debuffs():
    """matcher.buffType 索引必须能找到核心 debuff。"""
    matcher = _matcher()
    buff_idx = matcher["buffType"]
    direct = {
        "downAtk": "downAtk",
        "downDefence": "downDefence",
        "downCriticalrate": "downCriticalrate",
        "downCriticaldamage": "downCriticaldamage",
        "downNpdamage": "downNpdamage",
        "downDropnp": "downDropnp",
    }
    for buff_type, expected_effect in direct.items():
        assert expected_effect in buff_idx.get(buff_type, []), (
            f"buffType={buff_type} 应映射到 {expected_effect}, 实际={buff_idx.get(buff_type)}"
        )


def test_match_func_effects_recognizes_down_defence():
    matcher = _matcher()
    matched = _match_func_effects(_make_func("addStateShort", "downDefence"), matcher)
    assert "downDefence" in matched


def test_match_func_effects_recognizes_down_critical_rate():
    matcher = _matcher()
    matched = _match_func_effects(_make_func("addStateShort", "downCriticalrate"), matcher)
    assert "downCriticalrate" in matched


def test_match_func_effects_recognizes_down_attack():
    matcher = _matcher()
    matched = _match_func_effects(_make_func("addStateShort", "downAtk"), matcher)
    assert "downAtk" in matched


def test_card_resistance_validate_distinguishes_arts():
    """downCommandall + cardArts trait → 仅识别为 downArts。"""
    matcher = _matcher()
    func = _make_func(
        "addStateShort",
        "downCommandall",
        ck_self_indv=[{"id": 4001, "name": "cardArts"}],
    )
    matched = _match_func_effects(func, matcher)
    assert "downArts" in matched
    assert "downBuster" not in matched
    assert "downQuick" not in matched


def test_card_resistance_validate_distinguishes_buster():
    matcher = _matcher()
    func = _make_func(
        "addStateShort",
        "downCommandall",
        ck_self_indv=[{"id": 4002, "name": "cardBuster"}],
    )
    matched = _match_func_effects(func, matcher)
    assert "downBuster" in matched
    assert "downArts" not in matched
    assert "downQuick" not in matched


def test_card_resistance_validate_distinguishes_quick():
    matcher = _matcher()
    func = _make_func(
        "addStateShort",
        "downCommandall",
        ck_self_indv=[{"id": 4003, "name": "cardQuick"}],
    )
    matched = _match_func_effects(func, matcher)
    assert "downQuick" in matched
    assert "downArts" not in matched
    assert "downBuster" not in matched


def test_debuff_damage_class_b_for_def_and_atk():
    """downDefence / downAtk 必须标记为 B 类乘区。"""
    matcher = _matcher()
    dc = matcher["damageClasses"]
    assert dc.get("downDefence") == "B"
    assert dc.get("downAtk") == "B"


def test_debuff_damage_class_a_for_card_resistance():
    """downArts / downBuster / downQuick 必须标记为 A 类乘区。"""
    matcher = _matcher()
    dc = matcher["damageClasses"]
    for n in ("downArts", "downBuster", "downQuick"):
        assert dc.get(n) == "A"


def test_down_tolerance_recognized():
    matcher = _matcher()
    matched = _match_func_effects(_make_func("addStateShort", "downTolerance"), matcher)
    assert "downTolerance" in matched
