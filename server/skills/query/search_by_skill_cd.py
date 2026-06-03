"""Skill: 按技能冷却时间（CD）筛选从者。

支持纯 CD 筛选和效果+CD 联合匹配（同一技能粒度内）。
CD 取值为 Lv10 满级 CD（skillDetails[].coolDown）。
"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

_OP_MAP = {
    "lt": lambda cd, val: cd < val,
    "lte": lambda cd, val: cd <= val,
    "eq": lambda cd, val: cd == val,
    "gte": lambda cd, val: cd >= val,
    "gt": lambda cd, val: cd > val,
}


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    op: str = Field(default="lte", description="比较操作: lt/lte/eq/gte/gt")
    value: int = Field(description="CD 阈值（回合数）")
    effect: str | None = Field(
        default=None,
        description="效果名锚点，限定只匹配拥有此效果的技能的 CD",
    )
    target_type: str | None = Field(
        default=None,
        alias="targetType",
        description="目标类型锚点: self/party/enemy 等",
    )


def _compare(cool_down: int, op: str, value: int) -> bool:
    """比较 CD 值与阈值。"""
    comparator = _OP_MAP.get(op)
    if comparator is None:
        return False
    return comparator(cool_down, value)


def _skill_has_effect(skill: dict, effect: str, target_type: str | None) -> bool:
    """检查单个技能是否拥有指定效果（含目标类型锚点）。"""
    for eff in skill.get("effects", []):
        if eff.get("type") != effect:
            continue
        if target_type and eff.get("targetType") != target_type:
            continue
        return True
    return False


@register_skill
class SearchBySkillCd(QuerySkill):
    name = "search_by_skill_cd"
    description = "按技能冷却时间筛选从者，支持效果+CD联合匹配"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        op = params.get("op", "lte")
        value = params.get("value")
        effect = params.get("effect")
        target_type = params.get("target_type")

        if value is None:
            return True

        for skill in servant.get("skillDetails", []):
            cool_down = skill.get("coolDown", 0)
            if cool_down <= 0:
                continue

            if not _compare(cool_down, op, value):
                continue

            # CD 条件满足
            if effect is None:
                return True

            # 有 effect 参数：同一技能内检查效果匹配
            if _skill_has_effect(skill, effect, target_type):
                return True

        return False
