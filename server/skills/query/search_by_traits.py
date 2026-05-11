"""Skill: 按特性筛选从者（支持中文特性名或 ID）。"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from server.individuality import filter_by_traits
from server.skills.base import QuerySkill, register_skill

# ── 中文特性名 → trait ID 反查缓存 ──
_TRAIT_NAME_TO_ID: dict[str, int] = {}


def _ensure_trait_name_map() -> dict[str, int]:
    """懒加载中文特性名 → trait ID 映射。"""
    if _TRAIT_NAME_TO_ID:
        return _TRAIT_NAME_TO_ID
    mappings_path = Path(__file__).parent.parent.parent / "knowledge" / "mappings.json"
    if not mappings_path.exists():
        return _TRAIT_NAME_TO_ID
    with open(mappings_path, encoding="utf-8") as f:
        data = json.load(f)
    traits = data.get("traits", {})
    for tid_str, names in traits.items():
        try:
            tid = int(tid_str)
        except ValueError:
            continue
        cn = names.get("CN", "") or ""
        if cn:
            _TRAIT_NAME_TO_ID[cn] = tid
            # 去掉前缀"属性:"/"职阶:"/"性别:"后也加入映射，方便模糊匹配
            if ":" in cn:
                short = cn.split(":", 1)[1]
                if short:
                    _TRAIT_NAME_TO_ID[short] = tid
    return _TRAIT_NAME_TO_ID


def resolve_trait_names(names: list[str]) -> list[int]:
    """将中文特性名列表解析为 trait ID 列表。

    匹配策略：精确匹配 → 子串匹配（名称包含输入或输入包含名称）。
    """
    name_map = _ensure_trait_name_map()
    result: list[int] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        # 精确匹配
        if name in name_map:
            result.append(name_map[name])
            continue
        # 子串匹配
        found = False
        for cn, tid in name_map.items():
            if name in cn or cn in name:
                result.append(tid)
                found = True
                break
        if not found:
            # 尝试直接当 int 解析（兼容 LLM 直传 ID）
            try:
                result.append(int(name))
            except ValueError:
                pass
    return result


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    traits: list[int] | None = Field(default=None, description="包含特性 ID 列表")
    trait_names: list[str] | None = Field(
        default=None, alias="traitNames", description="中文特性名列表（如 ['龙', '王']）"
    )
    exclude_traits: list[int] | None = Field(default=None, alias="excludeTraits", description="排斥特性 ID 列表")


@register_skill
class SearchByTraits(QuerySkill):
    name = "search_by_traits"
    description = "按特性筛选从者（支持中文特性名，如龙、王、神性、活在当下的人类、兽科从者、圆桌骑士、秩序、混沌等）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        traits = params.get("traits")
        trait_names = params.get("trait_names")
        exclude_traits = params.get("exclude_traits")

        # 中文特性名 → ID 转换
        if trait_names:
            resolved_ids = resolve_trait_names(trait_names)
            if resolved_ids:
                traits = (traits or []) + resolved_ids

        if not traits and not exclude_traits:
            return True
        servant_traits = servant.get("traits", [])
        return filter_by_traits(servant_traits, traits, exclude_traits)
