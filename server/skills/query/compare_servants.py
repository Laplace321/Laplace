"""Skill: 多从者对比查询。

使用 lookup_servant 共享的 find_servant_candidates() 进行名称匹配，
支持执行层多候选 Clarification 检测。
"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill
from server.skills.query.lookup_servant import find_servant_candidates


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    names: list[str] = Field(description="要对比的从者名称列表")


@register_skill
class CompareServants(QuerySkill):
    name = "compare_servants"
    description = "按名称查询多个从者进行对比"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """分别查找每个名称对应的从者，保留所有候选。

        当某个 name 匹配到多个候选时，取星级最高者作为代表。
        多候选歧义由 SkillExecutor 层的 clarification 机制处理。
        """
        names = params.get("names", [])
        if not names:
            return []

        results = []
        seen_ids: set[int] = set()
        for name in names:
            candidates = find_servant_candidates(db, name)
            if not candidates:
                continue
            # 取星级最高者作为代表（歧义检测在 Executor 层统一处理）
            best = max(candidates, key=lambda s: (s.get("rarity", 0), -s.get("collectionNo", 0)))
            if best["id"] not in seen_ids:
                seen_ids.add(best["id"])
                results.append(best)

        return results
