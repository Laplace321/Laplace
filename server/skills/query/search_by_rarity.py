"""
Query Skill: search_by_rarity

按稀有度筛选从者。
迁移自 query_executor.py _filter_rarity。
"""

from pydantic import BaseModel

from server.query_executor import _compare
from server.skills.base import QuerySkill, register_skill


class RarityParams(BaseModel):
    """稀有度查询参数。"""

    op: str = "eq"
    value: int = 0


@register_skill
class SearchByRarity(QuerySkill):
    name = "search_by_rarity"
    description = "按稀有度筛选从者（如：五星从者、三星以上）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return RarityParams

    @property
    def prompt_fragment(self) -> str:
        return "稀有度条件。op 可选 eq/gte/lte/gt/lt。value 为星级数值（0-5）。"

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {"input": "五星从者", "output": '{"op": "eq", "value": 5}'},
            {"input": "三星以上", "output": '{"op": "gte", "value": 3}'},
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        rarity = servant.get("rarity", 0)
        op = params.get("op", "eq")
        value = params.get("value", 0)
        return _compare(rarity, op, value)
