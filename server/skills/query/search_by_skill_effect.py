"""Skill: 按技能效果筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str | None = Field(default=None, alias="skillEffect", description="单效果名")
    effects: list[str] | None = Field(default=None, alias="skillEffects", description="多效果列表")
    effects_op: str = Field(default="and", alias="skillEffectsOp", description="多效果组合: and/or")
    target_type: str | None = Field(default=None, alias="targetType", description="目标类型: self/party/enemy")


@register_skill
class SearchBySkillEffect(QuerySkill):
    name = "search_by_skill_effect"
    description = "按技能效果筛选从者（如无敌、回血、NP 充能等）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        effect = params.get("effect")
        effects = params.get("effects")
        target_type = params.get("target_type")

        # 单效果模式
        if effect is not None:
            return _match_effect(servant, effect, target_type)

        # 多效果模式
        if effects is not None and isinstance(effects, list):
            op = params.get("effects_op", "and").lower()
            if op == "or":
                return any(_match_effect(servant, eff, target_type) for eff in effects)
            else:
                return all(_match_effect(servant, eff, target_type) for eff in effects)

        return True
