"""Skill: 按 ATK/HP 类型筛选概念礼装。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

# ATK 类型中文别名映射
_ATK_TYPE_ALIASES: dict[str, str] = {
    "纯攻": "pure_atk",
    "纯atk": "pure_atk",
    "攻击型": "pure_atk",
    "纯血": "pure_hp",
    "纯hp": "pure_hp",
    "防御型": "pure_hp",
    "混合": "mixed",
    "均衡": "mixed",
    "平衡型": "mixed",
    "pure_atk": "pure_atk",
    "pure_hp": "pure_hp",
    "mixed": "mixed",
}


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    atk_type: str = Field(description="ATK 类型: pure_atk / pure_hp / mixed")


@register_skill
class CESearchByAtkType(QuerySkill):
    name = "ce_search_by_atk_type"
    description = "按 ATK/HP 类型筛选概念礼装（纯攻/纯血/混合）"
    domain = "ce"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, ce: dict, params: dict) -> bool:
        query_type = params.get("atk_type", "").strip().lower()
        # 中文别名解析
        resolved_type = _ATK_TYPE_ALIASES.get(query_type, query_type)
        ce_type = ce.get("atkType", "")
        return ce_type == resolved_type
