"""Skill: 多从者对比查询。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _normalize_text, load_nicknames
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    names: list[str] = Field(description="要对比的从者名称列表")


def _find_servant_by_name(db: list[dict], name: str) -> dict | None:
    """在数据库中按名称查找单个从者。"""
    query = name.strip()
    normalized_query = _normalize_text(query)

    # 昵称映射
    nicknames = load_nicknames()
    mapped_data = None
    for nick, data in nicknames.items():
        if _normalize_text(nick) == normalized_query:
            mapped_data = data
            break

    mapped_name = None
    extra_filters = {}
    if isinstance(mapped_data, str):
        mapped_name = mapped_data.lower()
    elif isinstance(mapped_data, dict):
        mapped_name = mapped_data.get("name", "").lower()
        for k, v in mapped_data.items():
            if k != "name":
                extra_filters[k] = v

    for servant in db:
        # 额外过滤器
        skip = False
        for attr, val in extra_filters.items():
            if attr == "className":
                if servant.get("className", "").lower() != val.lower():
                    skip = True
                    break
        if skip:
            continue

        en_name = servant.get("name", "").lower()
        cn_name = servant.get("aliasCN", "").lower()
        jp_name = servant.get("originalName", "").lower()
        norm_en = _normalize_text(en_name)
        norm_cn = _normalize_text(cn_name)
        norm_jp = _normalize_text(jp_name)

        # 精确匹配（映射名）
        if mapped_name:
            norm_mapped = _normalize_text(mapped_name)
            if norm_mapped in (norm_en, norm_cn, norm_jp):
                return servant

        # 子串模糊匹配
        if len(normalized_query) >= 2:
            if normalized_query in norm_en or normalized_query in norm_cn or normalized_query in norm_jp:
                return servant

        # 反向子串匹配
        if (
            (norm_en and norm_en in normalized_query)
            or (norm_cn and norm_cn in normalized_query)
            or (norm_jp and norm_jp in normalized_query)
        ):
            return servant

    return None


@register_skill
class CompareServants(QuerySkill):
    name = "compare_servants"
    description = "按名称查询多个从者进行对比"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """重写 execute：分别查找每个名称对应的从者。"""
        names = params.get("names", [])
        if not names:
            return []

        results = []
        seen_ids: set[int] = set()
        for name in names:
            servant = _find_servant_by_name(db, name)
            if servant is not None and servant["id"] not in seen_ids:
                seen_ids.add(servant["id"])
                results.append(servant)

        return results
