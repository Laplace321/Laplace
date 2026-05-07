"""Skill: 按名称查询单个从者（精确/模糊/昵称）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _normalize_text, load_nicknames
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(description="从者名称（支持中/英/日/昵称）")


@register_skill
class LookupServant(QuerySkill):
    name = "lookup_servant"
    description = "按名称查询单个从者（支持中英日名和昵称）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        query_name = params.get("name")
        if query_name is None or not isinstance(query_name, str) or not query_name.strip():
            return True

        query_name = query_name.strip()
        normalized_query = _normalize_text(query_name)

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

        # 额外过滤器（如职阶）
        for attr, val in extra_filters.items():
            if attr == "className":
                if servant.get("className", "").lower() != val.lower():
                    return False

        en_name = servant.get("name", "").lower()
        cn_name = servant.get("aliasCN", "").lower()
        jp_name = servant.get("originalName", "").lower()
        norm_en = _normalize_text(en_name)
        norm_cn = _normalize_text(cn_name)
        norm_jp = _normalize_text(jp_name)

        # 阶段 1: 精确匹配
        if mapped_name:
            norm_mapped = _normalize_text(mapped_name)
            if norm_mapped in (norm_en, norm_cn, norm_jp):
                return True

        # 阶段 2: 子串模糊匹配
        if len(normalized_query) >= 2:
            if normalized_query in norm_en or normalized_query in norm_cn or normalized_query in norm_jp:
                return True

        # 阶段 3: 反向子串匹配
        if (
            (norm_en and norm_en in normalized_query)
            or (norm_cn and norm_cn in normalized_query)
            or (norm_jp and norm_jp in normalized_query)
        ):
            return True

        return False
