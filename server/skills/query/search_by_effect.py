"""Skill: 按效果统一筛选从者（同时搜技能效果 + 宝具效果）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect
from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _expand_effect, _resolve_effect_name


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
        min_value: 效果最小数值（万分比），None 表示不限
        max_value: 效果最大数值（万分比），None 表示不限
    """
    hit_skill = source in ("both", "skill") and _match_effect(servant, effect_name, target_type, min_value, max_value)
    # npEffects 目前是纯效果名集合，不含 targetType/value 维度数据（Phase 8 再扩展）。
    # 当用户指定了精细条件时，宝具端无法校验，必须跳过，避免误匹配。
    has_quantitative_filter = target_type is not None or min_value is not None or max_value is not None
    hit_np = source in ("both", "np") and not has_quantitative_filter and effect_name in servant.get("npEffects", [])
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
        # 百分比 → 万分比转换（LLM 传 50 表示 50%，内部用 5000）
        raw_min = params.get("min_value")
        raw_max = params.get("max_value")
        min_value = raw_min * 100 if raw_min is not None else None
        max_value = raw_max * 100 if raw_max is not None else None

        # 单效果模式（支持复合效果自动展开为 OR）
        if effect is not None:
            expanded = _expand_effect(effect)
            if len(expanded) > 1:
                return any(_check_effect(servant, eff, source, target_type, min_value, max_value) for eff in expanded)
            return _check_effect(servant, expanded[0], source, target_type, min_value, max_value)

        # 多效果模式
        if effects is not None and isinstance(effects, list):
            resolved = [_resolve_effect_name(eff) for eff in effects]
            op = params.get("effects_op", "and").lower()
            if op == "or":
                return any(_check_effect(servant, eff, source, target_type, min_value, max_value) for eff in resolved)
            else:
                return all(_check_effect(servant, eff, source, target_type, min_value, max_value) for eff in resolved)

        return True
