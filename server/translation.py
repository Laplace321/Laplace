"""
翻译与预消化模块 — 将英文枚举值转换为中文用户语言。

从 main.py 抽取，提供：
- 翻译映射缓存访问（className / npCard / npTarget）
- 效果代码→中文翻译
- skill_calls→人类可读筛选条件描述
"""

import json
from pathlib import Path

from server.config_loader import CachedConfig
from server.skills.base import SKILL_REGISTRY

_translations_cache = CachedConfig(Path(__file__).parent / "config" / "translations.json")


def get_class_map() -> dict:
    """获取职阶英文→中文翻译映射。"""
    return _translations_cache.get()["className"]


def get_np_card_map() -> dict:
    """获取宝具卡色英文→中文翻译映射。"""
    return _translations_cache.get()["npCard"]


def get_np_target_map() -> dict:
    """获取宝具目标英文→中文翻译映射。"""
    return _translations_cache.get()["npTarget"]


_effect_map: dict | None = None
_trait_map: dict[int, str] | None = None


def get_trait_translation(trait_id: int) -> str | None:
    """将 trait ID 翻译为中文名称。首次调用时加载并缓存映射。

    Returns:
        中文名称；查不到或为 'None' 时返回 None。
    """
    global _trait_map
    if _trait_map is None:
        _trait_map = {}
        mappings_path = Path(__file__).parent / "knowledge" / "mappings.json"
        if mappings_path.exists():
            with open(mappings_path, encoding="utf-8") as f:
                data = json.load(f)
                for k, v in (data.get("traits") or {}).items():
                    cn = v.get("CN") if isinstance(v, dict) else None
                    if cn and cn != "None":
                        try:
                            _trait_map[int(k)] = cn
                        except (TypeError, ValueError):
                            continue
    return _trait_map.get(int(trait_id)) if trait_id is not None else None


def translate_traits(trait_ids: list, *, exclude_meta: bool = True) -> list[str]:
    """批量翻译 trait ID 列表，过滤无映射项。

    Args:
        trait_ids: trait ID 整数列表
        exclude_meta: 是否过滤掉在 entry 其他字段已呈现的元数据
            （性别/职阶/主副属性/稀有度/“从者”通用标签），
            默认 True。
    """
    META_PREFIXES = ("性别:", "职阶:", "属性:", "副属性:", "★")
    META_EXACT = {"从者"}
    out: list[str] = []
    for tid in trait_ids or []:
        cn = get_trait_translation(tid)
        if not cn:
            continue
        if exclude_meta and (cn.startswith(META_PREFIXES) or cn in META_EXACT):
            continue
        out.append(cn)
    return out


def get_effect_translation(effect_code: str) -> str:
    """将效果代码翻译为中文名称。首次调用时加载并缓存映射。"""
    global _effect_map
    if _effect_map is None:
        _effect_map = {}
        schema_path = Path(__file__).parent / "knowledge" / "effect_schema.json"
        if schema_path.exists():
            with open(schema_path, encoding="utf-8") as f:
                data = json.load(f)
                from server.data_loader import merge_effect_overlay

                effects = merge_effect_overlay(data.get("effects", []))
                for effect in effects:
                    name = effect.get("name")
                    aliases = effect.get("aliases_zh", [])
                    if name and aliases:
                        _effect_map[name] = aliases[0]
    return _effect_map.get(effect_code, effect_code)


def effect_qualifier(params: dict) -> str:
    """根据效果参数生成中文前缀修饰语（目标类型+数值条件）。"""
    parts: list[str] = []
    target_type = params.get("targetType") or params.get("target_type")
    if target_type == "party":
        parts.append("全队的")
    elif target_type == "partyOther":
        parts.append("仅队友的")
    elif target_type == "self":
        parts.append("自身的")
    elif target_type == "ally":
        parts.append("队友的")
    elif target_type == "ptOne":
        parts.append("单体队友的")
    elif target_type == "enemy":
        parts.append("对敌方的")

    min_val = params.get("minValue") or params.get("min_value")
    max_val = params.get("maxValue") or params.get("max_value")
    if min_val is not None and max_val is not None:
        parts.append(f"{min_val}%~{max_val}%")
    elif min_val is not None:
        parts.append(f"≥{min_val}%")
    elif max_val is not None:
        parts.append(f"≤{max_val}%")

    return "".join(parts)


def describe_filters(skill_calls: list[dict]) -> list[str]:
    """将 skill_calls 转换为人类可读的筛选条件描述列表。

    例如: ["技能效果包含「Arts提升」", "稀有度 = 5星"]
    供 LLM 在生成回复时明确知道系统做了什么筛选。
    """
    descriptions: list[str] = []
    for call in skill_calls:
        name = call.get("skill_name", "")
        params = call.get("params", {})
        if name == "search_by_effect":
            effect = params.get("effect", "")
            effects = params.get("effects")
            source = params.get("source", "both")
            qualifier = effect_qualifier(params)
            max_cd = params.get("maxCd") or params.get("max_cd")
            cd_suffix = f" + CD ≤ {max_cd}回合" if max_cd else ""
            effects_op = params.get("effectsOp", "and")
            # 多效果数组
            if effects and isinstance(effects, list):
                translated_list = [get_effect_translation(e) for e in effects]
                joiner = " / " if effects_op == "or" else " + "
                translated = joiner.join(translated_list)
            elif effect:
                translated = get_effect_translation(effect)
            else:
                translated = ""
            if not effect and not effects and max_cd:
                # 纯 CD 查询
                descriptions.append(f"技能CD ≤ {max_cd}回合")
            elif source == "skill":
                descriptions.append(f"技能效果包含「{qualifier}{translated}」{cd_suffix}")
            elif source == "np":
                descriptions.append(f"宝具效果包含「{qualifier}{translated}」")
            else:
                descriptions.append(f"效果包含「{qualifier}{translated}」{cd_suffix}")
        elif name == "search_by_skill_effect":
            effect = params.get("skillEffect") or params.get("effect", "")
            effects = params.get("effects") or params.get("skillEffects")
            qualifier = effect_qualifier(params)
            max_cd = params.get("maxCd") or params.get("max_cd")
            cd_suffix = f" + CD ≤ {max_cd}回合" if max_cd else ""
            effects_op = params.get("effectsOp") or params.get("skillEffectsOp") or "and"
            if effects and isinstance(effects, list):
                translated_list = [get_effect_translation(e) for e in effects]
                joiner = " / " if effects_op == "or" else " + "
                translated = joiner.join(translated_list)
            elif effect:
                translated = get_effect_translation(effect)
            else:
                translated = ""
            if not effect and not effects and max_cd:
                descriptions.append(f"技能CD ≤ {max_cd}回合")
            else:
                descriptions.append(f"技能效果包含「{qualifier}{translated}」{cd_suffix}")
        elif name == "search_by_np_effect":
            effect = params.get("npEffect") or params.get("effect", "")
            effects = params.get("effects") or params.get("npEffects")
            effects_op = params.get("effectsOp") or params.get("npEffectsOp") or "and"
            if effects and isinstance(effects, list):
                translated_list = [get_effect_translation(e) for e in effects]
                joiner = " / " if effects_op == "or" else " + "
                translated = joiner.join(translated_list)
            elif effect:
                translated = get_effect_translation(effect)
            else:
                translated = ""
            descriptions.append(f"宝具效果包含「{translated}」")
        elif name == "search_by_rarity":
            op = params.get("op", "eq")
            val = params.get("value", "")
            op_map = {"eq": "=", "gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
            descriptions.append(f"稀有度 {op_map.get(op, op)} {val}星")
        elif name == "search_by_class":
            descriptions.append(f"职阶 = {params.get('className', '')}")
        elif name == "search_by_cards":
            parts = []
            card = params.get("cardType") or params.get("cards")
            np_card = params.get("npCard") or params.get("np_card")
            np_target = params.get("npTarget") or params.get("np_target")
            if card:
                parts.append(f"配卡包含「{card}」")
            if np_card:
                np_card_map_local = get_np_card_map()
                parts.append(f"宝具卡色 = {np_card_map_local.get(np_card.lower(), np_card)}")
            if np_target:
                target_map = {"all": "全体(光炮)", "one": "单体", "support": "辅助"}
                parts.append(f"宝具目标 = {target_map.get(np_target.lower(), np_target)}")
            descriptions.append(" + ".join(parts) if parts else "配卡筛选")
        elif name == "search_by_class_advantage":
            target = params.get("targetClass") or params.get("target_class", "")
            descriptions.append(f"克制「{target}」职阶")
        elif name == "search_by_traits":
            trait_names = params.get("traitNames") or params.get("trait_names") or []
            ascension = params.get("ascension")
            asc_label = ""
            if ascension is not None:
                asc_map = {0: "初始灵基", 1: "灵基一", 2: "灵基二", 3: "灵基三", 4: "最终再临"}
                asc_label = f"（{asc_map.get(ascension, f'灵基{ascension}')}）"
            if trait_names:
                for t in trait_names:
                    descriptions.append(f"特性包含「{t}」{asc_label}")
            else:
                trait = params.get("trait", "")
                if trait:
                    descriptions.append(f"特性包含「{trait}」{asc_label}")
        elif name == "search_by_attribute":
            attr = params.get("attribute", "")
            descriptions.append(f"属性 = {attr}")
        elif name == "compare_servants":
            names = params.get("names", [])
            descriptions.append(f"对比从者「{'」与「'.join(names)}」")
        elif name == "lookup_servant":
            query = params.get("name") or params.get("query", "")
            descriptions.append(f"查询从者「{query}」")
        elif name == "resolve_nickname":
            nick = params.get("name", "")
            descriptions.append(f"智能识别昵称「{nick}」")
        elif name == "coronation_knowledge":
            topic = params.get("topic", "通用")
            class_name = params.get("className")
            if topic == "boss" and class_name:
                descriptions.append(f"戴冠战Boss机制查询: {class_name}阶")
            else:
                descriptions.append(f"戴冠战知识查询: {topic}")
        elif name == "coronation_team":
            class_name = params.get("className", "")
            role = params.get("role")
            playstyle = params.get("playstyle")
            desc = f"戴冠战配队推荐: {class_name}阶"
            if role:
                desc += f" - {role}"
            if playstyle:
                desc += f" - {playstyle}流"
            descriptions.append(desc)
        elif name == "ce_lookup":
            query = params.get("name", "")
            descriptions.append(f"查询礼装「{query}」")
        elif name == "ce_search_by_effect":
            effect = params.get("effect", "")
            translated = get_effect_translation(effect) if effect else effect
            lb = params.get("limit_break", True)
            lb_label = "满破" if lb else "未满破"
            descriptions.append(f"礼装效果包含「{translated}」（{lb_label}）")
        elif name == "ce_search_by_rarity":
            op = params.get("op", "eq")
            val = params.get("value", "")
            op_map = {"eq": "=", "gte": "≥", "lte": "≤", "gt": ">", "lt": "<"}
            descriptions.append(f"礼装稀有度 {op_map.get(op, op)} {val}星")
        elif name == "ce_search_by_atk_type":
            atk_type = params.get("atk_type", "")
            type_map = {"pure_atk": "纯攻型", "pure_hp": "纯血型", "mixed": "混合型"}
            descriptions.append(f"礼装类型 = {type_map.get(atk_type, atk_type)}")
        elif name == "ce_search_by_obtain":
            obtain = params.get("obtain_type", "")
            obtain_map = {
                "permanent": "常驻池",
                "limited": "限定",
                "event": "活动配布",
                "shop": "稀有棱柱兑换",
                "bond": "羁绊礼装",
                "valentine": "情人节礼装",
                "exp": "经验值礼装",
            }
            descriptions.append(f"礼装获取方式 = {obtain_map.get(obtain, obtain)}")
        else:
            # 兜底：仅输出 Skill 中文 description，禁止暴露参数结构
            skill_instance = SKILL_REGISTRY.get(name)
            if skill_instance:
                descriptions.append(skill_instance.description)
            else:
                descriptions.append(f"筛选条件: {name}")
    return descriptions
