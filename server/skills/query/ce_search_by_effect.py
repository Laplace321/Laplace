"""Skill: 按效果搜索概念礼装（满破效果优先）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str = Field(description="效果名（如 gainNp, upBuster, invincible 等）")
    limit_break: bool = Field(default=True, description="是否搜索满破效果（默认 True）")


@register_skill
class CESearchByEffect(QuerySkill):
    name = "ce_search_by_effect"
    description = "按效果搜索概念礼装（默认搜满破效果）"
    domain = "ce"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, ce: dict, params: dict) -> bool:
        effect_name = params.get("effect", "")
        limit_break = params.get("limit_break", True)

        if not effect_name:
            return False

        # 选择满破或未满破效果列表
        if limit_break:
            effects = ce.get("effectsLimitBreak", [])
        else:
            effects = ce.get("effects", [])

        return effect_name in effects
