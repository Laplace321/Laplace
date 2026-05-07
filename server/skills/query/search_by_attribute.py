"""
Query Skill: search_by_attribute

按性别和阵营筛选从者。
迁移自 query_executor.py _filter_gender + _filter_attribute。
"""

from typing import Literal

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill


class AttributeParams(BaseModel):
    """性别 + 阵营查询参数。"""

    gender: Literal["male", "female", "unknown"] | None = None
    attribute: Literal["earth", "sky", "human", "star", "beast"] | None = None


@register_skill
class SearchByAttribute(QuerySkill):
    name = "search_by_attribute"
    description = "按性别或阵营筛选从者"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return AttributeParams

    @property
    def prompt_fragment(self) -> str:
        return "性别筛选：male/female/unknown。阵营筛选：earth(地)/sky(天)/human(人)/star(星)/beast(兽)。"

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {
                "input": "女性从者",
                "output": '{"gender": "female", "attribute": null}',
            },
            {
                "input": "天属性从者",
                "output": '{"gender": null, "attribute": "sky"}',
            },
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        gender = params.get("gender")
        if gender is not None:
            if servant.get("gender", "") != gender:
                return False

        attribute = params.get("attribute")
        if attribute is not None:
            if servant.get("attribute", "") != attribute:
                return False

        return True
