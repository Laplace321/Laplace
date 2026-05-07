"""Skill: 按职阶筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    class_name: str = Field(alias="className")


@register_skill
class SearchByClass(QuerySkill):
    name = "search_by_class"
    description = "按职阶筛选从者（如 Saber、Caster）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        class_name = params.get("class_name")
        if class_name is None:
            return True
        return servant.get("className", "").lower() == class_name.lower()
