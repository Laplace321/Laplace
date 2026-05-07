"""
Query Skill: search_by_np_charge

按 NP 充能条件筛选从者。
迁移自 query_executor.py _filter_np_charge。
"""

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill

CompareOp = str  # "eq" | "gte" | "lte" | "gt" | "lt"


class NpChargeParams(BaseModel):
    """NP 充能查询参数。"""

    op: str = "eq"
    value: int = 0


@register_skill
class SearchByNpCharge(QuerySkill):
    name = "search_by_np_charge"
    description = "按 NP 自充能力筛选从者（如：30自充以上的从者）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return NpChargeParams

    @property
    def prompt_fragment(self) -> str:
        return (
            "NP 充能条件。op 可选 eq(等于)、gte(大于等于)、lte(小于等于)、"
            "gt(大于)、lt(小于)。value 是百分比数值（如 30 表示 30%）。"
            "eq 匹配单条充能记录的 chargePercent，其余匹配 totalCharge。"
        )

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {"input": "30自充的从者", "output": '{"op": "eq", "value": 30}'},
            {"input": "50自充以上的从者", "output": '{"op": "gte", "value": 50}'},
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        if not servant.get("hasNpCharge", False):
            return False
        charge = servant.get("totalCharge", 0)
        op = params.get("op", "eq")
        value = params.get("value", 0)
        if op == "eq":
            return any(c["chargePercent"] == value for c in servant.get("npCharges", []))
        elif op == "gte":
            return charge >= value
        elif op == "gt":
            return charge > value
        elif op == "lte":
            return charge <= value
        elif op == "lt":
            return charge < value
        return True
