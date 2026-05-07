"""
Query Skill: lookup_servant

按名称查询从者（精确匹配 → 子串模糊 → 昵称映射）。
迁移自 query_executor.py _filter_name。
"""

from pydantic import BaseModel

from server.query_executor import _normalize_text, load_nicknames
from server.skills.base import QuerySkill, register_skill


class LookupParams(BaseModel):
    """从者名称查询参数。"""

    name: str


@register_skill
class LookupServant(QuerySkill):
    name = "lookup_servant"
    description = "按名称查询从者（支持昵称、模糊匹配）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return LookupParams

    @property
    def prompt_fragment(self) -> str:
        return "从者名称关键词。支持中文名、英文名、日文名、社区昵称。保留用户原文，不要擅自改写。"

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {"input": "呆毛", "output": '{"name": "呆毛"}'},
            {"input": "千子村正的技能", "output": '{"name": "千子村正"}'},
        ]

    def filter(self, servant: dict, params: dict) -> bool:
        query_name = params.get("name", "").strip()
        if not query_name:
            return True

        normalized_query = _normalize_text(query_name)

        # 尝试昵称转换
        nicknames = load_nicknames()
        mapped_data = None
        for nick, data in nicknames.items():
            if _normalize_text(nick) == normalized_query:
                mapped_data = data
                break

        # 处理昵称映射
        mapped_name = None
        extra_filters = {}
        if isinstance(mapped_data, str):
            mapped_name = mapped_data.lower()
        elif isinstance(mapped_data, dict):
            mapped_name = mapped_data.get("name", "").lower()
            for k, v in mapped_data.items():
                if k != "name":
                    extra_filters[k] = v

        # 检查额外过滤器（如职阶）
        for attr, val in extra_filters.items():
            if attr == "className":
                if servant.get("className", "").lower() != val.lower():
                    return False

        en_name = servant.get("name", "").lower()
        cn_name = servant.get("aliasCN", "").lower()
        jp_name = servant.get("originalName", "").lower()
        normalized_en = _normalize_text(en_name)
        normalized_cn = _normalize_text(cn_name)
        normalized_jp = _normalize_text(jp_name)

        # 阶段 1: 精确匹配（有映射名时优先检查映射）
        if mapped_name:
            normalized_mapped = _normalize_text(mapped_name)
            if normalized_mapped in (normalized_en, normalized_cn, normalized_jp):
                return True

        # 阶段 2: 子串模糊匹配（"武尊" in "大和武尊"）
        if len(normalized_query) >= 2:
            if (
                normalized_query in normalized_en
                or normalized_query in normalized_cn
                or normalized_query in normalized_jp
            ):
                return True

        # 阶段 3: 反向子串匹配
        if (
            (normalized_en and normalized_en in normalized_query)
            or (normalized_cn and normalized_cn in normalized_query)
            or (normalized_jp and normalized_jp in normalized_query)
        ):
            return True

        return False
