"""Skill: 按宝具效果筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill
from server.skills.query.search_by_skill_effect import _expand_effect, _resolve_effect_name


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str | None = Field(default=None, alias="npEffect", description="单宝具效果名")
    effects: list[str] | None = Field(default=None, alias="npEffects", description="多宝具效果列表")
    effects_op: str = Field(default="and", alias="npEffectsOp", description="多宝具效果组合: and/or")


@register_skill
class SearchByNpEffect(QuerySkill):
    name = "search_by_np_effect"
    description = "按宝具效果筛选从者（如全体攻击、防御下降等）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        effect = params.get("effect")
        effects = params.get("effects")

        # 单效果模式（支持复合效果自动展开为 OR）
        if effect is not None:
            expanded = _expand_effect(effect)
            servant_np_effects = set(servant.get("npEffects", []))
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
