"""Skill: 按名称/昵称查找概念礼装。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _normalize_text, load_ce_nicknames
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(description="礼装名称或昵称（中文/英文/日文均可）")


@register_skill
class CELookup(QuerySkill):
    name = "ce_lookup"
    description = "按名称或昵称查找概念礼装"
    domain = "ce"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        query_name = params.get("name", "").strip()
        if not query_name:
            return []

        normalized_query = _normalize_text(query_name)

        # 1. 先尝试昵称解析
        nicknames = load_ce_nicknames()
        resolved_name = None
        for nick, target in nicknames.items():
            if _normalize_text(nick) == normalized_query:
                resolved_name = target
                break

        # 2. 精确匹配（name / nameCn / 昵称解析后的英文名）
        results = []
        for ce in db:
            ce_name_en = _normalize_text(ce.get("name", ""))
            ce_name_cn = _normalize_text(ce.get("nameCn", ""))

            # 精确匹配
            if normalized_query in (ce_name_en, ce_name_cn):
                results.append(ce)
                continue

            # 昵称解析后精确匹配
            if resolved_name and _normalize_text(resolved_name) == ce_name_en:
                results.append(ce)
                continue

        if results:
            return results

        # 3. 子串匹配（模糊搜索）
        for ce in db:
            ce_name_en = _normalize_text(ce.get("name", ""))
            ce_name_cn = _normalize_text(ce.get("nameCn", ""))

            if normalized_query in ce_name_en or normalized_query in ce_name_cn:
                results.append(ce)
            elif resolved_name and _normalize_text(resolved_name) in ce_name_en:
                results.append(ce)

        return results
