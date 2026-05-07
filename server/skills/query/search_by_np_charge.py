"""Skill: 按 NP 充能量筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    op: str = Field(default="gte", description="比较操作符: eq/gte/gt/lte")
    value: int = Field(default=30, alias="charge_value", description="NP 充能百分比")


@register_skill
class SearchByNpCharge(QuerySkill):
    name = "search_by_np_charge"
    description = "按 NP 充能量筛选从者（如自充 ≥ 50%）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        if not servant.get("hasNpCharge", False):
            return False
        charge = servant.get("totalCharge", 0)
        op = params.get("op", "gte")
        value = params.get("value", 30)
        if op == "eq":
            return any(c["chargePercent"] == value for c in servant.get("npCharges", []))
        elif op == "gte":
            return charge >= value
        elif op == "gt":
            return charge > value
        elif op == "lte":
            return charge <= value
        return True
