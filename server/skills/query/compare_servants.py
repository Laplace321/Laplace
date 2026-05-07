"""
Query Skill: compare_servants

多从者对比查询。
迁移自 query_executor.py execute_query 中的 names 分支。
"""

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill


class CompareParams(BaseModel):
    """多从者对比参数。"""

    names: list[str]


@register_skill
class CompareServants(QuerySkill):
    name = "compare_servants"
    description = "对比多个从者的属性和能力"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return CompareParams

    @property
    def prompt_fragment(self) -> str:
        return "从者名称数组（2 个以上）。保留用户原文，不要擅自改写昵称。"

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {
                "input": "对比千子村正和大和武尊",
                "output": '{"names": ["千子村正", "大和武尊"]}',
            },
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        """compare_servants 不使用 filter，而是覆盖 execute。"""
        return True

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """逐个查询每个从者名称，取每个名称的第一个匹配结果。"""
        names = params.get("names", [])
        if not names:
            return []

        # 导入 lookup_servant 的 filter 逻辑来复用名称匹配
        from server.skills.query.lookup_servant import LookupServant

        lookup = LookupServant()
        all_results = []
        seen_ids: set[int] = set()

        for name in names:
            lookup_params = {"name": name}
            for servant in db:
                if lookup.filter(servant, lookup_params):
                    if servant["id"] not in seen_ids:
                        seen_ids.add(servant["id"])
                        all_results.append(servant)
                    break  # 每个名称只取第一个匹配

        # 按稀有度降序 → collectionNo 升序排序
        all_results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))
        return all_results
