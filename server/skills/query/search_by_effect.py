"""Skill: 按效果统一筛选从者（同时搜技能效果 + 宝具效果）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect
from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _resolve_effect_name


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


def _check_effect(
    servant: dict,
    effect_name: str,
    source: str,
    target_type: str | None,
) -> bool:
    """检查从者是否拥有特定效果（支持按来源筛选）。

    Args:
        servant: 从者数据
        effect_name: 效果名（英文 key）
        source: 搜索来源 - skill / np / both
        target_type: 目标类型筛选，None 表示不限
    """
    hit_skill = source in ("both", "skill") and _match_effect(servant, effect_name, target_type)
    hit_np = source in ("both", "np") and effect_name in servant.get("npEffects", [])
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

        # 单效果模式
        if effect is not None:
            resolved = _resolve_effect_name(effect)
            return _check_effect(servant, resolved, source, target_type)

        # 多效果模式
        if effects is not None and isinstance(effects, list):
            resolved = [_resolve_effect_name(eff) for eff in effects]
            op = params.get("effects_op", "and").lower()
            if op == "or":
                return any(_check_effect(servant, eff, source, target_type) for eff in resolved)
            else:
                return all(_check_effect(servant, eff, source, target_type) for eff in resolved)

        return True
