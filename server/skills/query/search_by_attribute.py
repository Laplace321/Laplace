"""Skill: 按阵营属性筛选从者。"""

from pydantic import BaseModel, ConfigDict

from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    attribute: str


@register_skill
class SearchByAttribute(QuerySkill):
    name = "search_by_attribute"
    description = "按阵营属性筛选从者（earth/sky/human/star/beast）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        attribute = params.get("attribute")
        if attribute is None:
            return True
        return servant.get("attribute", "") == attribute
