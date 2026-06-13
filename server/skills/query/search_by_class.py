"""Skill: 按职阶筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill
from server.translation import get_class_map


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    class_name: str = Field(alias="className")


@register_skill
class SearchByClass(QuerySkill):
    name = "search_by_class"
    description = "按职阶筛选从者，支持中文（如「狂阶」「术阶」）或英文（如 Saber、Caster）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        class_name = params.get("class_name")
        if class_name is None:
            return True
        # 防御层：LLM 可能输出中文（「狂阶」），需反查回英文（berserker）再与 DB 比较。
        # 见 ADR / hotfix v0.4.5。
        raw = class_name.strip()
        cn_to_en = {cn: en for en, cn in get_class_map().items()}
        target = cn_to_en.get(raw, raw)
        return servant.get("className", "").lower() == target.lower()
