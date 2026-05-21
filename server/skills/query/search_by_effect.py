"""Skill: 按效果统一筛选从者（同时搜技能效果 + 宝具效果）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect, _match_np_effect
from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _expand_effect

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
