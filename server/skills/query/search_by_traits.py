"""Skill: 按特性筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.individuality import filter_by_traits
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    traits: list[int] | None = Field(default=None, description="包含特性 ID 列表")
    exclude_traits: list[int] | None = Field(default=None, alias="excludeTraits", description="排斥特性 ID 列表")


@register_skill
class SearchByTraits(QuerySkill):
    name = "search_by_traits"
    description = "按特性 ID 筛选从者（如龙、王等特性）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        traits = params.get("traits")
        exclude_traits = params.get("exclude_traits")
        if not traits and not exclude_traits:
            return True
        servant_traits = servant.get("traits", [])
        return filter_by_traits(servant_traits, traits, exclude_traits)
