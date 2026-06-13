"""Skill: 按宝具效果筛选从者（支持效果名匹配和数值阈值筛选）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _expand_effect, _resolve_effect_name

# 宝具伤害倍率 npValues 使用千分比（如 6000=600%），用户传百分比（如 400=400%），乘 10 转换
_NP_DAMAGE_MULTIPLIER = 10


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str | None = Field(default=None, alias="npEffect", description="单宝具效果名")
    effects: list[str] | None = Field(default=None, alias="npEffects", description="多宝具效果列表")
    effects_op: str = Field(default="and", alias="npEffectsOp", description="多宝具效果组合: and/or")
    min_value: int | None = Field(default=None, alias="minValue", description="效果最小数值（百分比，如400表示≥400%）")
    max_value: int | None = Field(default=None, alias="maxValue", description="效果最大数值（百分比）")


def _match_np_value(servant: dict, effect_name: str, min_val: int | None, max_val: int | None) -> bool:
    """检查从者的宝具效果是否满足数值阈值条件。

    对 npDamage 使用 npValues[4]（NP5 倍率）作为参考值；
    对 damageNpSP（宝具特攻）npValues 存储的是 OC1 特攻倍率（Correction，千分比，
    如 1500=150%），取 npValues[0] 即 OC1 特攻倍率；
    其他效果使用 npValues[0]（NP1 OC1 数值）。
    """
    for np_detail in servant.get("npDetails", []):
        for eff in np_detail.get("effects", []):
            if eff.get("type") != effect_name:
                continue
            np_vals = eff.get("npValues", [])
            # npDamage 取 NP5（最大倍率），其他效果取 NP1
            if effect_name == "npDamage":
                value = np_vals[4] if len(np_vals) >= 5 else (np_vals[0] if np_vals else 0)
            else:
                value = np_vals[0] if np_vals else 0
            if min_val is not None and value < min_val:
                continue
            if max_val is not None and value > max_val:
                continue
            return True
    return False


@register_skill
class SearchByNpEffect(QuerySkill):
    name = "search_by_np_effect"
    description = "按宝具效果筛选从者（如全体攻击、防御下降、宝具倍率等）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        effect = params.get("effect")
        effects = params.get("effects")
        raw_min = params.get("min_value")
        raw_max = params.get("max_value")

        # 转换用户百分比到内部千分比
        min_val = raw_min * _NP_DAMAGE_MULTIPLIER if raw_min is not None else None
        max_val = raw_max * _NP_DAMAGE_MULTIPLIER if raw_max is not None else None

        # 单效果模式（支持复合效果自动展开为 OR）
        if effect is not None:
            expanded = _expand_effect(effect)
            servant_np_effects = set(servant.get("npEffects", []))

            # 有数值条件时，使用 npDetails 精确匹配
            if min_val is not None or max_val is not None:
                if len(expanded) > 1:
                    return any(_match_np_value(servant, eff, min_val, max_val) for eff in expanded)
                return _match_np_value(servant, expanded[0], min_val, max_val)

            # 无数值条件，快速集合匹配
            if len(expanded) > 1:
                return any(eff in servant_np_effects for eff in expanded)
            return expanded[0] in servant_np_effects

        # 多效果模式
        if effects is not None and isinstance(effects, list):
            resolved = [_resolve_effect_name(eff) for eff in effects]
            servant_np_effects = set(servant.get("npEffects", []))
            op = params.get("effects_op", "and").lower()
            if op == "or":
                return any(eff in servant_np_effects for eff in resolved)
            else:
                return all(eff in servant_np_effects for eff in resolved)

        return True
