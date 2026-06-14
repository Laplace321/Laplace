"""Skill: 按效果统一筛选从者（同时搜技能效果 + 宝具效果）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect, _match_np_effect
from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _expand_effect
from server.skills.query.search_by_traits import resolve_trait_names

# 效果类型→percentBase 映射（参考 Chaldea const_data.dart）
# - base=100: gainNp 系 FuncType（Value÷100=百分比，如 3000=30%）
# - base=10: Buff 类效果（Value÷10=百分比，如 500=50%）
# - base=None: 整数值效果（如 gainStar，Value=实际颗数）
_PERCENT_BASE_100_EFFECTS = frozenset({"gainNp", "regainNp"})
_NO_PERCENT_EFFECTS = frozenset({"gainStar"})


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str | None = Field(default=None, alias="effect", description="单效果名")
    effects: list[str] | None = Field(default=None, alias="effects", description="多效果列表")
    effects_op: str = Field(default="and", alias="effectsOp", description="多效果组合: and/or")
    source: str = Field(
        default="both",
        alias="source",
        description="搜索来源: skill(仅技能) / np(仅宝具) / both(默认，同时搜)",
    )
    target_type: str | None = Field(
        default=None,
        alias="targetType",
        description="目标类型: self(自身)/party(全队含自己)/partyOther(队友不含自己)/ptOne(仅单体队友)/enemy",
    )
    min_value: int | None = Field(default=None, alias="minValue", description="效果最小数值（百分比，如50表示≥50%）")
    max_value: int | None = Field(default=None, alias="maxValue", description="效果最大数值（百分比）")
    max_cd: int | None = Field(default=None, alias="maxCd", description="技能CD上限（回合数），同一技能内联合校验")
    anti_trait: str | None = Field(
        default=None,
        alias="antiTrait",
        description="特攻目标特性名（中文，如'龙'、'神性'、'王'），仅在查询特攻(upDamage/damageNpSP)时使用",
    )


def _convert_value(
    effect_name: str,
    raw_min: int | None,
    raw_max: int | None,
) -> tuple[int | None, int | None]:
    """将用户传入的百分比数值转换为内部 Value 单位。

    FGO Atlas API 中不同效果类型使用不同的数值基数（参考 Chaldea const_data.dart）：
    - gainNp/regainNp: base=100（Value÷100=百分比，如 3000=30%）→ 用户传50 × 100 = 5000
    - gainStar: 整数值（Value=实际颗数）→ 不转换
    - Buff 类（upAtk/upBuster 等）: base=10（Value÷10=百分比，如 500=50%）→ 用户传50 × 10 = 500
    """
    if raw_min is None and raw_max is None:
        return None, None

    if effect_name in _PERCENT_BASE_100_EFFECTS:
        multiplier = 100
    elif effect_name in _NO_PERCENT_EFFECTS:
        multiplier = 1
    else:
        multiplier = 10

    min_value = raw_min * multiplier if raw_min is not None else None
    max_value = raw_max * multiplier if raw_max is not None else None
    return min_value, max_value


def _check_effect(
    servant: dict,
    effect_name: str,
    source: str,
    target_type: str | None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> bool:
    """检查从者是否拥有特定效果（支持按来源、目标类型和数值筛选）。

    Args:
        servant: 从者数据
        effect_name: 效果名（英文 key）
        source: 搜索来源 - skill / np / both
        target_type: 目标类型筛选，None 表示不限
        min_value: 效果最小数值（千分比‰），None 表示不限
        max_value: 效果最大数值（千分比‰），None 表示不限
    """
    hit_skill = source in ("both", "skill") and _match_effect(servant, effect_name, target_type, min_value, max_value)
    hit_np = source in ("both", "np") and _match_np_effect(servant, effect_name, target_type, min_value, max_value)
    return hit_skill or hit_np


def _match_skill_with_cd(
    servant: dict,
    effect_name: str | None,
    target_type: str | None,
    min_value: int | None,
    max_value: int | None,
    max_cd: int,
) -> bool:
    """单技能粒度联合匹配：在同一个技能内同时检查效果+数值+CD。

    当 effect_name 为 None 时（纯 CD 查询），只检查技能 CD ≤ max_cd。
    """
    for skill in servant.get("skillDetails", []):
        cool_down = skill.get("coolDown", 0)
        if cool_down <= 0 or cool_down > max_cd:
            continue

        # CD 条件满足
        if effect_name is None:
            return True

        # 检查同一技能内的效果匹配
        for eff in skill.get("effects", []):
            if eff.get("type") != effect_name:
                continue
            if target_type and eff.get("targetType") != target_type:
                continue
            # 数值校验
            if min_value is not None or max_value is not None:
                value_max = eff.get("valueMax", 0)
                if min_value is not None and value_max < min_value:
                    continue
                if max_value is not None and value_max > max_value:
                    continue
            return True

    return False


def _match_anti_trait(servant: dict, anti_trait: str, source: str = "both") -> bool:
    """检查从者的 antiTraitIndex 是否包含指定特攻目标特性。

    优先通过 resolve_trait_names 解析为 traitId 精确匹配，
    若无法解析则退化为特性名子串匹配。

    Args:
        servant: 从者数据
        anti_trait: 用户输入的特攻目标特性名（中文，如"龙"）
        source: 搜索来源 - skill / np / both
    """
    anti_index = servant.get("antiTraitIndex")
    if not anti_index:
        return False

    # 尝试解析为 trait ID（精确匹配）
    resolved_ids = resolve_trait_names([anti_trait])
    target_id = resolved_ids[0] if resolved_ids else None

    for entry in anti_index:
        # 来源过滤
        entry_source = entry.get("source", "")
        if source == "skill" and entry_source != "skill":
            continue
        if source == "np" and entry_source != "np":
            continue

        # 优先按 traitId 匹配
        if target_id is not None:
            if entry.get("traitId") == target_id:
                return True
        else:
            # 退化为特性名子串匹配
            entry_trait = entry.get("trait", "")
            if anti_trait in entry_trait or entry_trait in anti_trait:
                return True

    return False


@register_skill
class SearchByEffect(QuerySkill):
    name = "search_by_effect"
    description = "按效果筛选从者，默认同时搜技能效果和宝具效果"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        effect = params.get("effect")
        effects = params.get("effects")
        source = params.get("source", "both")
        target_type = params.get("target_type")
        raw_min = params.get("min_value")
        raw_max = params.get("max_value")
        max_cd = params.get("max_cd")
        anti_trait = params.get("anti_trait")

        # ── 特攻目标特性过滤（antiTrait 前置检查） ──
        # 当指定了 antiTrait 时，从者必须在 antiTraitIndex 中有对应条目
        if anti_trait:
            if not _match_anti_trait(servant, anti_trait, source):
                return False

        # ── CD 联合匹配路径（同一技能粒度内） ──
        # 当 max_cd 有值时，必须在单个技能内同时满足效果+数值+CD
        # source="np" 时 CD 无意义（宝具没有冷却），退化为普通效果匹配
        if max_cd is not None and source != "np":
            if effect is not None:
                expanded = _expand_effect(effect)
                # 复合效果展开为 OR：任一子效果+CD 命中即可
                for sub_eff in expanded:
                    sub_min, sub_max = _convert_value(sub_eff, raw_min, raw_max)
                    if _match_skill_with_cd(servant, sub_eff, target_type, sub_min, sub_max, max_cd):
                        return True
                return False
            # 纯 CD 查询（effect=None）
            return _match_skill_with_cd(servant, None, None, None, None, max_cd)

        # ── 标准效果匹配路径（无 CD 约束，行为不变） ──

        # 单效果模式（支持复合效果自动展开为 OR）
        if effect is not None:
            expanded = _expand_effect(effect)
            if len(expanded) > 1:
                return any(
                    _check_effect(servant, eff, source, target_type, *_convert_value(eff, raw_min, raw_max))
                    for eff in expanded
                )
            min_value, max_value = _convert_value(expanded[0], raw_min, raw_max)
            return _check_effect(servant, expanded[0], source, target_type, min_value, max_value)

        # 多效果模式（每个效果都可能是复合效果，需展开）
        if effects is not None and isinstance(effects, list):
            op = params.get("effects_op", "and").lower()

            def _match_one(eff_name: str) -> bool:
                """单个效果匹配（支持复合效果展开为 OR）。"""
                expanded = _expand_effect(eff_name)
                if len(expanded) > 1:
                    # 复合效果：子效果之间是 OR（任一命中即可）
                    return any(
                        _check_effect(servant, sub, source, target_type, *_convert_value(sub, raw_min, raw_max))
                        for sub in expanded
                    )
                min_v, max_v = _convert_value(expanded[0], raw_min, raw_max)
                return _check_effect(servant, expanded[0], source, target_type, min_v, max_v)

            if op == "or":
                return any(_match_one(eff) for eff in effects)
            else:
                return all(_match_one(eff) for eff in effects)

        return True
