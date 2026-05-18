"""Skill: 按获取方式筛选概念礼装。"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

# 加载获取方式别名配置
_OBTAIN_MAP_PATH = Path(__file__).parent.parent.parent / "config" / "ce_obtain_map.json"
_obtain_aliases: dict[str, str] = {}

if _OBTAIN_MAP_PATH.exists():
    with open(_OBTAIN_MAP_PATH, encoding="utf-8") as f:
        _config = json.load(f)
    _obtain_aliases = _config.get("obtain_type_aliases", {})


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    obtain_type: str = Field(description="获取方式: permanent/limited/event/bond/valentine")


@register_skill
class CESearchByObtain(QuerySkill):
    name = "ce_search_by_obtain"
    description = "按获取方式筛选概念礼装（常驻/限定/活动/羁绊/情人节）"
    domain = "ce"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, ce: dict, params: dict) -> bool:
        query_type = params.get("obtain_type", "").strip()
        # 中文别名解析
        resolved_type = _obtain_aliases.get(query_type, query_type)
        ce_obtain = ce.get("obtain", "")
        return ce_obtain == resolved_type
