"""Skill: 按 NP 充能量筛选从者。

支持 targetType 区分充能类型：
- 不传 targetType（默认）→ 查 totalCharge（自充+他充+群充总和）
- "self"  → 查 maxSelfCharge（纯自充）
- "ptOne" → 查 maxPtOneCharge（他充，指定单个队友）
- "ptAll" → 查 maxPtAllCharge（群充，全队含自己）

语义约定：
- 用户只说"自充"时，默认查 totalCharge（因为群充/他充也能给自己充）
- 用户同时提到"自充"和"群充"/"他充"时，分别用精确 targetType 查询
"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

# targetType → 数据库字段名 的映射
_TARGET_TYPE_FIELD_MAP = {
    "self": "maxSelfCharge",
    "ptOne": "maxPtOneCharge",
    "ptAll": "maxPtAllCharge",
}


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    op: str = Field(default="gte", description="比较操作符: eq/gte/gt/lte")
    value: int = Field(default=30, alias="charge_value", description="NP 充能百分比")
    targetType: str | None = Field(
        default=None,
        description="充能类型: self=纯自充, ptOne=他充, ptAll=群充; 不传则查总充能",
    )


@register_skill
class SearchByNpCharge(QuerySkill):
    name = "search_by_np_charge"
    description = "按 NP 充能量筛选从者（支持 targetType 区分自充/他充/群充）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        if not servant.get("hasNpCharge", False):
            return False

        target_type = params.get("targetType")
        field_name = _TARGET_TYPE_FIELD_MAP.get(target_type, "totalCharge") if target_type else "totalCharge"
        charge = servant.get(field_name, 0)

        op = params.get("op", "gte")
        value = params.get("value", 30)
        if op == "eq":
            return charge == value
        if op == "gte":
            return charge >= value
        if op == "gt":
            return charge > value
        if op == "lte":
            return charge <= value
        return True
