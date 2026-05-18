"""Skill: 按稀有度筛选概念礼装。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _compare
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    op: str = Field(default="eq", description="比较操作符: eq/gte/gt/lte/lt")
    value: int = Field(description="稀有度值(1-5)")


@register_skill
class CESearchByRarity(QuerySkill):
    name = "ce_search_by_rarity"
    description = "按稀有度筛选概念礼装（支持精确和范围比较）"
    domain = "ce"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, ce: dict, params: dict) -> bool:
        op = params.get("op", "eq")
        value = params.get("value", 0)
        rarity = ce.get("rarity", 0)
        return _compare(rarity, op, value)
