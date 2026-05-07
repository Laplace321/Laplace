"""
Query Skill: search_by_traits

按特性（Trait）筛选从者。
迁移自 query_executor.py _filter_traits。
"""

from pydantic import BaseModel

from server.individuality import filter_by_traits
from server.skills.base import QuerySkill, register_skill


class TraitsParams(BaseModel):
    """特性查询参数。"""

    traits: list[int] = []
    exclude_traits: list[int] = []


@register_skill
class SearchByTraits(QuerySkill):
    name = "search_by_traits"
    description = "按特性筛选从者（如：秩序善、龙属性、神性）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return TraitsParams

    @property
    def prompt_fragment(self) -> str:
        return (
            "特性 ID 筛选。traits 为必须拥有的特性 ID 数组。"
            "exclude_traits 为排斥特性 ID 数组。"
            "常见特性：秩序=300, 混沌=301, 中立=302, 善=303, 恶=304, "
            "中庸=305, 狂=306, 夏=308, 神性=2000, 龙=2002, 罗马=2004。"
        )

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {
                "input": "秩序善的从者",
                "output": '{"traits": [300, 303], "exclude_traits": []}',
            },
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        traits = params.get("traits", [])
        exclude_traits = params.get("exclude_traits", [])
        if not traits and not exclude_traits:
            return True
        servant_traits = servant.get("traits", [])
        return filter_by_traits(servant_traits, traits or None, exclude_traits or None)
