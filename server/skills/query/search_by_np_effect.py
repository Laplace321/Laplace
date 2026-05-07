"""
Query Skill: search_by_np_effect

按宝具效果筛选从者。
迁移自 query_executor.py _filter_np_effect + _filter_np_effects。
"""

from typing import Literal

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill


class NpEffectParams(BaseModel):
    """宝具效果查询参数。"""

    effects: list[str]
    effects_op: Literal["and", "or"] = "and"


@register_skill
class SearchByNpEffect(QuerySkill):
    name = "search_by_np_effect"
    description = "按宝具效果筛选从者（如：宝具有加攻效果的）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return NpEffectParams

    @property
    def prompt_fragment(self) -> str:
        return (
            "宝具效果筛选。effects 为宝具效果名数组。"
            "effects_op 控制逻辑：'and' 必须同时拥有，'or' 满足其一。"
            "注意：只有用户明确说'宝具有XX效果'时才使用此 Skill。"
        )

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {
                "input": "宝具有加攻效果的从者",
                "output": '{"effects": ["upAtk"], "effects_op": "and"}',
            },
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        effects = params.get("effects", [])
        if not effects:
            return True
        servant_np_effects = set(servant.get("npEffects", []))
        effects_op = params.get("effects_op", "and")

        if effects_op == "or":
            return any(eff in servant_np_effects for eff in effects)
        else:
            return all(eff in servant_np_effects for eff in effects)
