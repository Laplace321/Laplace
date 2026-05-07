"""
Query Skill: search_by_skill_effect

按技能效果筛选从者（支持单效果、多效果 AND/OR、目标类型筛选）。
迁移自 query_executor.py _filter_skill_effect + _filter_skill_effects。
"""

from typing import Literal

from pydantic import BaseModel

from server.query_executor import _match_effect
from server.skills.base import QuerySkill, register_skill


class SkillEffectParams(BaseModel):
    """技能效果查询参数。"""

    effects: list[str]
    effects_op: Literal["and", "or"] = "and"
    target_type: Literal["self", "party", "enemy"] | None = None


@register_skill
class SearchBySkillEffect(QuerySkill):
    name = "search_by_skill_effect"
    description = "按技能效果筛选从者（如：有无敌技能的、有回避和毅力的）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return SkillEffectParams

    @property
    def prompt_fragment(self) -> str:
        return (
            "技能效果筛选。effects 为效果名数组（如 ['invincible', 'guts']）。"
            "effects_op 控制多效果逻辑：'and' 必须同时拥有，'or' 满足其一。"
            "target_type 筛选效果目标：'self' 自身、'party' 全体己方、'enemy' 敌方。"
        )

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {
                "input": "有无敌技能的从者",
                "output": '{"effects": ["invincible"], "effects_op": "and", "target_type": null}',
            },
            {
                "input": "有回避或毅力的从者",
                "output": '{"effects": ["avoidance", "guts"], "effects_op": "or", "target_type": null}',
            },
            {
                "input": "能给全队加攻的从者",
                "output": '{"effects": ["upAtk"], "effects_op": "and", "target_type": "party"}',
            },
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        effects = params.get("effects", [])
        if not effects:
            return True
        effects_op = params.get("effects_op", "and")
        target_type = params.get("target_type")

        if effects_op == "or":
            return any(_match_effect(servant, eff, target_type) for eff in effects)
        else:
            return all(_match_effect(servant, eff, target_type) for eff in effects)
