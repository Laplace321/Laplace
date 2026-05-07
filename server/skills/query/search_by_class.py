"""
Query Skill: search_by_class

按职阶筛选从者。
迁移自 query_executor.py _filter_class。
"""

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill


class ClassParams(BaseModel):
    """职阶查询参数。"""

    class_name: str


@register_skill
class SearchByClass(QuerySkill):
    name = "search_by_class"
    description = "按职阶筛选从者（如：Saber、Caster）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return ClassParams

    @property
    def prompt_fragment(self) -> str:
        return (
            "职阶名称（小写英文）。支持：saber, archer, lancer, rider, caster, "
            "assassin, berserker, ruler, avenger, moonCancer, alterEgo, foreigner, pretender。"
        )

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {"input": "剑阶从者", "output": '{"class_name": "saber"}'},
            {"input": "术阶从者", "output": '{"class_name": "caster"}'},
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        class_name = params.get("class_name")
        if class_name is None:
            return True
        return servant.get("className", "").lower() == class_name.lower()
