"""Context 构建模块 — 从者数据预消化与 RAG Context 生成。

从 main.py 抽取的 Context 构建相关逻辑，包括：
- 效果详情格式化（_format_effect_detail）
- 技能/宝具详情构建（_build_skill_details / _build_np_details）
- 数值摘要提取（_extract_value_hints）
- 核心 Context 构建（build_context）
"""

from __future__ import annotations

from collections import Counter

from server.translation import (
    get_class_map,
    get_effect_translation,
    get_np_card_map,
    get_np_target_map,
)

# === 常量 ===

MAX_CONTEXT_SIZE = 5
MAX_RESULTS = 50

# ── targetType 中文映射 ──
TARGET_TYPE_MAP = {
    "self": "自身",
    "party": "全队",
    "partyOther": "队友",
    "ptOne": "单体（含自身）",
    "enemy": "敌方",
    "other": "其他",
}

# ── 效果数值类型分类集合 ──
_NP_PERCENT_EFFECTS = {"gainNp", "regainNp"}  # 万分比，/100
_STAR_EFFECTS = {"gainStar", "regainStar"}  # 直接数值，单位"个"
_HP_EFFECTS = {
    "gainHp",
    "regainHp",
    "addMaxhp",
    "guts",
    "reduceHp",
    "subSelfdamage",
}  # 直接数值
_DAMAGE_EFFECTS = {"addDamage", "damageNpSP"}  # 直接数值
_COUNT_EFFECTS = {"shortenSkill", "upChagetd"}  # 直接数值，无单位后缀


def _format_conditional_prefix(conditional: dict) -> str:
    """将 conditional 字段转为中文前缀标记。

    UseRate 是条件触发发动概率（千分比），缺失/None = 必定触发。
    1000=100% 为必定触发，省略概率描述。

    Args:
        conditional: {"triggerType": "回合结束时", "useRate": 5000}

    Returns:
        如 "[回合结束时]" 或 "[攻击后50%概率]"
    """
    trigger_type = conditional.get("triggerType", "")
    use_rate = conditional.get("useRate")

    # 概率描述：千分比→百分比，None 或 1000（100%）= 必定触发，省略
    rate_text = ""
    if use_rate is not None:
        rate_percent = use_rate // 10
        if rate_percent > 0 and rate_percent != 100:
            rate_text = f"{rate_percent}%概率"

    parts = [trigger_type]
    if rate_text:
        parts.append(rate_text)

    return f"[{''.join(parts)}]" if parts and any(parts) else ""


def format_effect_detail(eff: dict, is_np: bool = False) -> str:
    """将 effect dict 转为中文描述字符串（含数值、目标、持续）。

    条件触发效果（含 conditional 字段）会在描述前添加触发条件标记，
    如 "[回合结束时50%概率]弱化状态解除(自身)"。

    Args:
        eff: {"type": "upBuster", "targetType": "self", "valueMax": 500, "turn": 1, "count": -1}
        is_np: True 时使用 valueLv1 代替 valueMax

    Returns:
        如 "Buster提升(50%,自身,1T)" 或 "[延迟10回合50%概率]NP增加(100%,全队)"
    """
    effect_name = get_effect_translation(eff.get("type", ""))
    target = TARGET_TYPE_MAP.get(eff.get("targetType", ""), "")
    eff_type = eff.get("type", "")

    # 宝具效果：优先从 npValues[0] 取值（NP1 OC1），向后兼容 valueLv1
    if is_np:
        np_values = eff.get("npValues")
        raw_value = np_values[0] if np_values else eff.get("valueLv1", 0)
    else:
        raw_value = eff.get("valueMax", 0)

    parts: list[str] = []
    if raw_value and raw_value > 0:
        if eff_type in _NP_PERCENT_EFFECTS:
            parts.append(f"{raw_value / 100:.0f}%")
        elif eff_type in _STAR_EFFECTS:
            parts.append(f"{raw_value}个")
        elif eff_type in _HP_EFFECTS:
            parts.append(f"{raw_value}")
        elif eff_type in _DAMAGE_EFFECTS:
            parts.append(f"{raw_value}")
        elif eff_type in _COUNT_EFFECTS:
            parts.append(f"{raw_value}")
        else:
            # 默认：千分比 → 百分比
            parts.append(f"{raw_value / 10:.0f}%")

    if target:
        parts.append(target)

    turn = eff.get("turn", 0)
    count = eff.get("count", -1)
    if turn and turn > 0:
        parts.append(f"{turn}T")
    elif count and count > 0:
        parts.append(f"{count}次")

    base = f"{effect_name}({','.join(parts)})" if parts else effect_name

    # 宝具效果：追加 NP/OC 数值区间描述
    if is_np:
        range_parts: list[str] = []
        np_values = eff.get("npValues", [])
        oc_values = eff.get("ocValues", [])
        if np_values and len(np_values) >= 5 and np_values[0] != np_values[4]:
            range_parts.append(_format_value_range("NP", np_values[0], np_values[4], eff_type))
        if oc_values and len(oc_values) >= 5 and oc_values[0] != oc_values[4]:
            range_parts.append(_format_value_range("OC", oc_values[0], oc_values[4], eff_type))
        if range_parts:
            base += " " + " ".join(range_parts)

    # 条件触发效果：添加前缀标记
    conditional = eff.get("conditional")
    if conditional:
        prefix = _format_conditional_prefix(conditional)
        return f"{prefix}{base}" if prefix else base

    return base


def _format_value_range(dimension: str, val_min: int, val_max: int, eff_type: str) -> str:
    """格式化 NP/OC 维度的数值区间。"""
    if eff_type in _NP_PERCENT_EFFECTS:
        return f"({dimension}1:{val_min / 100:.0f}%→{dimension}5:{val_max / 100:.0f}%)"
    elif eff_type in _STAR_EFFECTS:
        return f"({dimension}1:{val_min}个→{dimension}5:{val_max}个)"
    elif eff_type in (_HP_EFFECTS | _DAMAGE_EFFECTS | _COUNT_EFFECTS):
        return f"({dimension}1:{val_min}→{dimension}5:{val_max})"
    else:
        return f"({dimension}1:{val_min / 10:.0f}%→{dimension}5:{val_max / 10:.0f}%)"


def build_skill_details(servant: dict) -> list[dict]:
    """构建单从者的技能详情（含数值），使用中文技能名或英文原名作为标签。

    将效果分为「效果」（普通效果）和「条件触发效果」两个列表，
    方便 LLM 区分确定性效果和概率/延迟触发效果。
    """
    result = []
    for sk in servant.get("skillDetails", []):
        normal_effects = []
        conditional_effects = []
        for eff in sk.get("effects", []):
            formatted = format_effect_detail(eff, is_np=False)
            if eff.get("conditional"):
                conditional_effects.append(formatted)
            else:
                normal_effects.append(formatted)
        if normal_effects or conditional_effects:
            label = sk.get("skillName", "")
            if not label:
                skill_num = sk.get("skillNum", 0)
                label = f"技能{skill_num}" if skill_num else "技能"
            entry: dict = {"技能名": label, "效果": normal_effects}
            if conditional_effects:
                entry["条件触发效果"] = conditional_effects
            result.append(entry)
    return result


def build_np_details(servant: dict) -> list[dict]:
    """构建单从者的宝具详情（含数值），使用中文宝具名或英文原名作为标签。

    多宝具从者（如卫宫红卡+蓝卡）时，每个宝具条目额外输出色卡和目标类型，
    帮助 LLM 区分不同宝具。
    """
    np_list = servant.get("npDetails", [])
    has_multiple_nps = len(np_list) > 1
    np_card_map = get_np_card_map() if has_multiple_nps else {}
    np_target_map = get_np_target_map() if has_multiple_nps else {}

    result = []
    for np_d in np_list:
        effects = []
        for eff in np_d.get("effects", []):
            effects.append(format_effect_detail(eff, is_np=True))
        if effects:
            label = np_d.get("npName", "") or "宝具"
            entry: dict = {"宝具名": label, "效果": effects}
            if has_multiple_nps:
                raw_card = np_d.get("npCard", "")
                raw_target = np_d.get("npTarget", "")
                entry["卡色"] = np_card_map.get(str(raw_card).lower(), raw_card)
                entry["目标"] = np_target_map.get(str(raw_target).lower(), raw_target)
            result.append(entry)
    return result


def match_target_type_display(query_type: str, data_type: str) -> bool:
    """匹配目标类型（展示层）。party 查询同时匹配 party（全队）和 ptOne（单体队友）。"""
    if query_type == data_type:
        return True
    if query_type == "party" and data_type == "ptOne":
        return True
    return False


def extract_value_hints(servant: dict, skill_calls: list[dict]) -> list[str]:
    """当 skill_calls 含数值条件时，提取该从者所有匹配效果的具体数值摘要。

    匹配条件：效果类型 + targetType（与查询条件一致），不做二次数值过滤。
    筛选阶段已保证该从者满足数值条件，这里只需列出所有同类效果供 LLM 展示。
    """
    from server.skills.query.search_by_skill_effect import _expand_effect

    hints: list[str] = []
    for call in skill_calls:
        name = call.get("skill_name", "")
        params = call.get("params", {})
        if name not in (
            "search_by_effect",
            "search_by_skill_effect",
            "search_by_np_effect",
        ):
            continue
        min_val = params.get("minValue") or params.get("min_value")
        max_val = params.get("maxValue") or params.get("max_value")
        if min_val is None and max_val is None:
            continue
        # 有数值条件 → 提取所有匹配效果类型的条目
        effect = params.get("effect") or params.get("skillEffect") or params.get("npEffect", "")
        target_type = params.get("targetType") or params.get("target_type")
        source = params.get("source", "both")
        expanded = _expand_effect(effect) if effect else [effect]
        # 从 skillDetails 提取（带技能名前缀）
        if source in ("both", "skill") and name != "search_by_np_effect":
            for sk in servant.get("skillDetails", []):
                sk_label = sk.get("skillName", "") or f"技能{sk.get('skillNum', '')}"
                for eff in sk.get("effects", []):
                    if eff.get("type") not in expanded:
                        continue
                    if target_type and not match_target_type_display(target_type, eff.get("targetType", "")):
                        continue
                    detail = format_effect_detail(eff, is_np=False)
                    hints.append(f"{sk_label}: {detail}")
        # 从 npDetails 提取（带宝具名前缀）
        if source in ("both", "np") and name != "search_by_skill_effect":
            for np_d in servant.get("npDetails", []):
                np_label = np_d.get("npName", "") or "宝具"
                for eff in np_d.get("effects", []):
                    if eff.get("type") not in expanded:
                        continue
                    if target_type and not match_target_type_display(target_type, eff.get("targetType", "")):
                        continue
                    detail = format_effect_detail(eff, is_np=True)
                    hints.append(f"{np_label}: {detail}")
    return hints


def build_context(
    servants: list[dict],
    detail_mode: bool = False,
    skill_calls: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    """构建预消化的精简 Context 供 RAG 生成使用。

    Args:
        servants: 匹配的从者列表
        detail_mode: True 时为单从者详情模式，输出效果数值/目标/回合数
        skill_calls: 路由解析的 Skill 调用列表，用于提取数值摘要

    Returns:
        (context_data, top_results) — context_data 含 total_found 等元信息；
        top_results 为翻译后的前 N 条详情。
    """
    total_found = len(servants)
    top_results: list[dict] = []

    # 翻译映射缓存
    class_map = get_class_map()
    np_card_map = get_np_card_map()
    np_target_map = get_np_target_map()

    for s in servants[:MAX_CONTEXT_SIZE]:
        raw_np_card = s.get("npCard")
        raw_np_target = s.get("npTarget")
        raw_class_name = s.get("className")
        raw_effects = s.get("skillEffects") or []
        raw_np_effects = s.get("npEffects") or []

        translated_effects = [get_effect_translation(e) for e in raw_effects]
        translated_np_effects = [get_effect_translation(e) for e in raw_np_effects]

        entry: dict = {
            "名称": s.get("name"),
            "中文名": s.get("aliasCN"),
            "职阶": class_map.get(str(raw_class_name).lower(), raw_class_name),
            "稀有度": s.get("rarity"),
            "配卡": s.get("cards"),
            "总充能": s.get("totalSelfCharge"),
            "宝具卡色": np_card_map.get(str(raw_np_card).lower(), raw_np_card),
            "宝具目标": np_target_map.get(str(raw_np_target).lower(), raw_np_target),
            "技能效果": translated_effects,
            "宝具效果": translated_np_effects,
        }
        # 详情模式：附带技能/宝具的数值、目标、回合数
        if detail_mode:
            skill_details = build_skill_details(s)
            np_details = build_np_details(s)
            if skill_details:
                entry["技能详情"] = skill_details
            if np_details:
                entry["宝具详情"] = np_details

        # 列表模式下：如果查询含数值条件，附带匹配效果的具体数值摘要
        if not detail_mode and skill_calls:
            value_hints = extract_value_hints(s, skill_calls)
            if value_hints:
                entry["查询效果数值"] = value_hints

        # 条件特性注释（仅在从者有条件特性时附带，供 LLM 在分析中说明）
        cond_traits = s.get("conditionalTraits", [])
        if cond_traits:
            cond_desc = []
            for ct in cond_traits:
                cond_type = ct.get("condType", "")
                cond_type_zh = {
                    "questClear": "关卡通关后",
                    "svtLimit": "指定灵基",
                }.get(cond_type, cond_type)
                cond_desc.append({"特性ID": ct["traitIds"], "条件": cond_type_zh})
            entry["条件特性"] = cond_desc
        top_results.append(entry)

    # 全局统计摘要（基于全部从者，而非仅 top N）
    stats_summary: dict = {}
    if total_found > MAX_CONTEXT_SIZE:
        np_card_dist = Counter(np_card_map.get(str(s.get("npCard", "")).lower(), s.get("npCard")) for s in servants)
        class_dist = Counter(class_map.get(str(s.get("className", "")).lower(), s.get("className")) for s in servants)
        rarity_dist = Counter(s.get("rarity") for s in servants)
        stats_summary = {
            "宝具卡色分布": dict(np_card_dist),
            "职阶分布": dict(class_dist),
            "稀有度分布": {f"{k}星": v for k, v in sorted(rarity_dist.items(), reverse=True)},
        }

    return {
        "匹配总数": total_found,
        "筛选条件": {},  # 由调用方填充
        "全局统计": stats_summary,
        "代表从者详情": top_results,
    }, top_results


# ============================================================
# 概念礼装 Context 构建
# ============================================================

_ATK_TYPE_CN = {
    "pure_atk": "纯攻型",
    "pure_hp": "纯血型",
    "mixed": "混合型",
    "zero": "零属性",
}

_OBTAIN_CN = {
    "permanent": "常驻池",
    "limited": "限定",
    "event": "活动配布",
    "shop": "稀有棱柱兑换",
    "bond": "羁绊礼装",
    "valentine": "情人节礼装",
    "exp": "经验值礼装",
}


def build_ce_context(
    craft_essences: list[dict],
    skill_calls: list[dict] | None = None,
) -> tuple[dict, list[dict]]:
    """构建概念礼装的预消化 Context 供 RAG 生成使用。

    Args:
        craft_essences: 匹配的礼装列表
        skill_calls: 路由解析的 Skill 调用列表

    Returns:
        (context_data, top_results)
    """
    total_found = len(craft_essences)
    top_results: list[dict] = []

    for ce in craft_essences[:MAX_CONTEXT_SIZE]:
        # 翻译效果列表
        raw_effects_lb = ce.get("effectsLimitBreak") or []
        translated_effects = [get_effect_translation(e) for e in raw_effects_lb]

        entry: dict = {
            "名称": ce.get("name"),
            "中文名": ce.get("nameCn") or ce.get("name"),
            "稀有度": ce.get("rarity"),
            "COST": ce.get("cost"),
            "类型": _ATK_TYPE_CN.get(ce.get("atkType", ""), ce.get("atkType", "")),
            "ATK": ce.get("atkMax", 0),
            "HP": ce.get("hpMax", 0),
            "获取方式": _OBTAIN_CN.get(ce.get("obtain", ""), ce.get("obtain", "")),
            "满破效果": translated_effects,
        }

        # NP 充能高亮
        np_charge = ce.get("npChargePercent", 0)
        if np_charge > 0:
            entry["NP充能"] = f"{np_charge}%"

        # 中文效果描述（满破优先）
        desc = ce.get("effectDescCnLB") or ce.get("effectDescCn", "")
        if desc:
            entry["效果描述"] = desc

        top_results.append(entry)

    # 全局统计摘要
    stats_summary: dict = {}
    if total_found > MAX_CONTEXT_SIZE:
        rarity_dist = Counter(ce.get("rarity") for ce in craft_essences)
        atk_type_dist = Counter(_ATK_TYPE_CN.get(ce.get("atkType", ""), ce.get("atkType", "")) for ce in craft_essences)
        stats_summary = {
            "稀有度分布": {f"{k}星": v for k, v in sorted(rarity_dist.items(), reverse=True)},
            "类型分布": dict(atk_type_dist),
        }

    return {
        "匹配总数": total_found,
        "筛选条件": {},  # 由调用方填充
        "全局统计": stats_summary,
        "代表礼装详情": top_results,
    }, top_results
