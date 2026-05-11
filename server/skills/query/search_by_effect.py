"""Skill: 按效果统一筛选从者（同时搜技能效果 + 宝具效果）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect, _match_np_effect
from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _expand_effect


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
    target_type: str | None = Field(default=None, alias="targetType", description="目标类型: self/party/enemy")
    min_value: int | None = Field(default=None, alias="minValue", description="效果最小数值（百分比，如50表示≥50%）")
    max_value: int | None = Field(default=None, alias="maxValue", description="效果最大数值（百分比）")


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
        # 百分比 → 千分比转换（LLM 传 50 表示 50%，内部用 500‰）
        raw_min = params.get("min_value")
        raw_max = params.get("max_value")
        min_value = raw_min * 10 if raw_min is not None else None
        max_value = raw_max * 10 if raw_max is not None else None

        # 单效果模式（支持复合效果自动展开为 OR）
        if effect is not None:
            expanded = _expand_effect(effect)
            if len(expanded) > 1:
                return any(_check_effect(servant, eff, source, target_type, min_value, max_value) for eff in expanded)
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
                        _check_effect(servant, sub, source, target_type, min_value, max_value) for sub in expanded
                    )
                return _check_effect(servant, expanded[0], source, target_type, min_value, max_value)

            if op == "or":
                return any(_match_one(eff) for eff in effects)
            else:
                return all(_match_one(eff) for eff in effects)

        return True
