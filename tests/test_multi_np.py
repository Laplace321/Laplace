"""多宝具从者全量解析 + 条件触发效果回归测试。

测试 _select_retained_nps() 的 num 分组去重逻辑，以及
extract_skill_effects() 对间接触发 buff（selfturnendFunction/delayFunction）的解析。
"""

from server.data_loader import (
    _CONDITIONAL_TRIGGER_BUFF_TYPES,
    _has_transform_servant,
    _select_retained_nps,
    extract_skill_effects,
)

# ── 辅助构造函数 ──


def _make_np(np_id: int, num: int = 1, card: int = 2, name: str = "NP") -> dict:
    """构造最小化的宝具数据。card: 1=arts, 2=buster, 3=quick"""
    return {"id": np_id, "num": num, "card": card, "name": name, "functions": []}


def _make_svt(noble_phantasms: list[dict], skills: list[dict] | None = None) -> dict:
    """构造最小化的从者数据。"""
    return {
        "noblePhantasms": noble_phantasms,
        "skills": skills or [],
    }


def _make_transform_skill() -> dict:
    """构造含 transformServant 的技能。"""
    return {
        "type": "active",
        "num": 3,
        "functions": [{"funcType": "transformServant"}],
    }


# ── _has_transform_servant 测试 ──


class TestHasTransformServant:
    def test_normal_servant_no_transform(self):
        """普通从者不含 transformServant。"""
        svt = _make_svt([], skills=[{"type": "active", "num": 1, "functions": [{"funcType": "gainNp"}]}])
        assert _has_transform_servant(svt) is False

    def test_servant_with_transform(self):
        """含 transformServant 技能的从者（如托勒密、妖兰）。"""
        svt = _make_svt([], skills=[_make_transform_skill()])
        assert _has_transform_servant(svt) is True

    def test_empty_skills(self):
        """无技能的从者。"""
        svt = _make_svt([])
        assert _has_transform_servant(svt) is False


# ── _select_retained_nps 测试 ──


class TestSelectRetainedNps:
    def test_normal_servant_single_np_after_strengthen(self):
        """普通从者（同 num=1 同名同色）只保留最后一个（强化后版本）。"""
        nps = [
            _make_np(100101, num=1, card=2, name="UBW"),
            _make_np(100102, num=1, card=2, name="UBW"),
        ]
        svt = _make_svt(nps)
        retained = _select_retained_nps(svt)

        assert len(retained) == 1
        assert retained[0]["id"] == 100102  # 强化后版本

    def test_emiya_three_nps_retained(self):
        """卫宫：num=1×2（B卡强化前/后）+ num=97 + num=98 → 保留 3 个。

        num=1 组只保留最后一个（强化后），num=97 和 num=98 各自独立保留。
        """
        nps = [
            _make_np(200101, num=1, card=2, name="UBW"),  # B卡强化前
            _make_np(200102, num=1, card=2, name="UBW"),  # B卡强化后
            _make_np(200197, num=97, card=1, name="UBW"),  # A卡变体
            _make_np(200198, num=98, card=1, name="UBW"),  # A卡变体
        ]
        svt = _make_svt(nps)
        retained = _select_retained_nps(svt)

        assert len(retained) == 3
        retained_ids = [np["id"] for np in retained]
        assert 200102 in retained_ids  # num=1 强化后
        assert 200197 in retained_ids  # num=97
        assert 200198 in retained_ids  # num=98
        assert 200101 not in retained_ids  # num=1 强化前被去掉

    def test_bb_dubai_two_nps_retained(self):
        """BB 迪拜：num=1（C.C.C.）+ num=98（G.G.G.）→ 保留 2 个。"""
        nps = [
            _make_np(2300601, num=1, card=1, name="C.C.C."),
            _make_np(2300698, num=98, card=1, name="G.G.G."),
        ]
        svt = _make_svt(nps)
        retained = _select_retained_nps(svt)

        assert len(retained) == 2
        retained_ids = [np["id"] for np in retained]
        assert 2300601 in retained_ids
        assert 2300698 in retained_ids

    def test_ptolemy_transform_all_retained(self):
        """托勒密：有 transformServant，同 num=1 的两个宝具全部保留。"""
        nps = [
            _make_np(205001, num=1, card=1, name="Pharos"),
            _make_np(205002, num=1, card=1, name="Bibliotheke"),
        ]
        svt = _make_svt(nps, skills=[_make_transform_skill()])
        retained = _select_retained_nps(svt)

        assert len(retained) == 2
        retained_ids = [np["id"] for np in retained]
        assert 205001 in retained_ids
        assert 205002 in retained_ids

    def test_space_ishtar_three_nps(self):
        """太空伊什塔尔：num=1 + num=97 + num=98 → 三个不同卡色变体全保留。"""
        nps = [
            _make_np(1100901, num=1, card=1, name="Edin"),  # A卡
            _make_np(1100997, num=97, card=2, name="Edin"),  # B卡
            _make_np(1100998, num=98, card=3, name="Edin"),  # Q卡
        ]
        svt = _make_svt(nps)
        retained = _select_retained_nps(svt)

        assert len(retained) == 3

    def test_empty_noble_phantasms(self):
        """无宝具的从者返回空列表。"""
        svt = _make_svt([])
        retained = _select_retained_nps(svt)
        assert retained == []

    def test_np_without_card_skipped(self):
        """无 card 字段的宝具被跳过。"""
        nps = [
            {"id": 999, "num": 1, "name": "NoCard", "functions": []},  # 无 card
            _make_np(100101, num=1, card=2, name="Valid"),
        ]
        svt = _make_svt(nps)
        retained = _select_retained_nps(svt)

        assert len(retained) == 1
        assert retained[0]["id"] == 100101


# ── 条件触发效果测试 ──


def _make_minimal_matcher() -> dict:
    """构造最小化的 matcher，只包含 gainNp 的匹配规则。"""
    return {
        "funcType": {"gainNp": ["gainNp"]},
        "buffType": {},
        "validates": {},
        "triggerBuffTypes": [],
    }


class TestConditionalTriggerEffects:
    """测试间接触发 buff（selfturnendFunction / delayFunction）的解析。"""

    def test_conditional_trigger_buff_types_constant(self):
        """确认间接触发 buff 类型常量包含预期值。"""
        assert "selfturnendFunction" in _CONDITIONAL_TRIGGER_BUFF_TYPES
        assert "delayFunction" in _CONDITIONAL_TRIGGER_BUFF_TYPES

    def test_salome_conditional_effect_parsed(self):
        """莎乐美3技能：selfturnendFunction 引用子 skill 962914（gainNp），应被正确解析。

        模拟数据结构：
        - func: addState + buff.type=selfturnendFunction
        - svals: Rate=5000(50%), Value=962914(子skill ID)
        - conditional_skill_map: {962914: [{funcType: gainNp, svals: [...]}]}
        """
        matcher = _make_minimal_matcher()

        # 模拟莎乐美的技能3：含 selfturnendFunction buff
        svt = {
            "skills": [
                {
                    "id": 648550,
                    "type": "active",
                    "num": 3,
                    "name": "Dance of the Seven Veils",
                    "coolDown": [7],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "self",
                            "buffs": [{"type": "selfturnendFunction"}],
                            "svals": [
                                {"Rate": 5000, "Turn": 7, "Count": -1, "Value": 962914},
                            ],
                        },
                    ],
                }
            ],
        }

        # 模拟 Atlas API 返回的子 skill 数据
        conditional_skill_map = {
            962914: [
                {
                    "funcType": "gainNp",
                    "funcTargetType": "self",
                    "buffType": "",
                    "buffs": [],
                    "svals": [
                        {"Rate": 1000, "Value": 10000},  # 100% NP
                    ],
                }
            ],
        }

        effects, details = extract_skill_effects(svt, matcher, conditional_skill_map)

        # gainNp 应被识别为技能效果
        assert "gainNp" in effects

        # skillDetails 中应有 conditional 字段
        skill3 = next(d for d in details if d["skillNum"] == 3)
        conditional_effects = [e for e in skill3["effects"] if e.get("conditional")]
        assert len(conditional_effects) >= 1

        cond_eff = conditional_effects[0]
        assert cond_eff["type"] == "gainNp"
        assert cond_eff["conditional"]["triggerType"] == "turnEnd"
        assert cond_eff["conditional"]["triggerRate"] == 5000

    def test_conditional_gainNp_not_in_total_charge(self):
        """条件触发的 gainNp 不应计入 totalSelfCharge。

        模拟一个从者同时有确定性自充（技能1: gainNp 30%）
        和条件触发自充（技能3: selfturnendFunction → gainNp 100%），
        totalSelfCharge 应只包含确定性的 30%。
        """
        matcher = _make_minimal_matcher()

        svt = {
            "skills": [
                {
                    "id": 100,
                    "type": "active",
                    "num": 1,
                    "name": "Certain Charge",
                    "coolDown": [6],
                    "functions": [
                        {
                            "funcType": "gainNp",
                            "funcTargetType": "self",
                            "buffs": [],
                            "svals": [
                                {"Rate": 1000, "Value": 3000},  # 30% NP (千分比)
                            ],
                        },
                    ],
                },
                {
                    "id": 200,
                    "type": "active",
                    "num": 3,
                    "name": "Conditional Charge",
                    "coolDown": [7],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "self",
                            "buffs": [{"type": "selfturnendFunction"}],
                            "svals": [
                                {"Rate": 5000, "Turn": 7, "Count": -1, "Value": 999},
                            ],
                        },
                    ],
                },
            ],
        }

        conditional_skill_map = {
            999: [
                {
                    "funcType": "gainNp",
                    "funcTargetType": "self",
                    "buffType": "",
                    "buffs": [],
                    "svals": [
                        {"Rate": 1000, "Value": 10000},  # 100% NP
                    ],
                }
            ],
        }

        effects, details = extract_skill_effects(svt, matcher, conditional_skill_map)

        # 两种 gainNp 都应在 skillEffects 中
        assert "gainNp" in effects

        # 计算 totalSelfCharge（模拟 build_database 逻辑）
        total_self_charge = 0
        for sk in details:
            for eff in sk.get("effects", []):
                if eff.get("type") != "gainNp":
                    continue
                if eff.get("conditional"):
                    continue
                tt = eff.get("targetType", "")
                charge_percent = eff.get("valueMax", 0) // 100
                if tt in ("self", "party", "ptOne"):
                    total_self_charge += charge_percent

        # 只计入确定性的 30%，不计入条件触发的 100%
        assert total_self_charge == 30

    def test_delay_function_parsed(self):
        """delayFunction 类型也应被正确解析。"""
        matcher = _make_minimal_matcher()

        svt = {
            "skills": [
                {
                    "id": 300,
                    "type": "active",
                    "num": 3,
                    "name": "Delayed Effect",
                    "coolDown": [8],
                    "functions": [
                        {
                            "funcType": "addState",
                            "funcTargetType": "self",
                            "buffs": [{"type": "delayFunction"}],
                            "svals": [
                                {"Rate": 5000, "Turn": 7, "Count": 1, "Value": 888, "Value2": 10},
                            ],
                        },
                    ],
                }
            ],
        }

        conditional_skill_map = {
            888: [
                {
                    "funcType": "gainNp",
                    "funcTargetType": "self",
                    "buffType": "",
                    "buffs": [],
                    "svals": [
                        {"Rate": 1000, "Value": 5000},
                    ],
                }
            ],
        }

        effects, details = extract_skill_effects(svt, matcher, conditional_skill_map)
        assert "gainNp" in effects

        skill3 = next(d for d in details if d["skillNum"] == 3)
        cond_effects = [e for e in skill3["effects"] if e.get("conditional")]
        assert len(cond_effects) >= 1
        assert cond_effects[0]["conditional"]["triggerType"] == "delayed"
        assert cond_effects[0]["conditional"]["triggerDelay"] == 10

    def test_no_conditional_map_backward_compatible(self):
        """不传 conditional_skill_map 时，行为与原逻辑完全一致。"""
        matcher = _make_minimal_matcher()

        svt = {
            "skills": [
                {
                    "id": 100,
                    "type": "active",
                    "num": 1,
                    "name": "Normal Charge",
                    "coolDown": [6],
                    "functions": [
                        {
                            "funcType": "gainNp",
                            "funcTargetType": "self",
                            "buffs": [],
                            "svals": [{"Rate": 1000, "Value": 3000}],
                        },
                    ],
                }
            ],
        }

        effects, details = extract_skill_effects(svt, matcher)
        assert "gainNp" in effects
        assert len(details) == 1
        # 无 conditional 字段
        for eff in details[0]["effects"]:
            assert "conditional" not in eff
