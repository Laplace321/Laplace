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


# FGO 阵营两轴（用于组合字符串拆解）
_ALIGNMENT_AXIS1 = ["秩序", "混沌", "中立"]
_ALIGNMENT_AXIS2 = ["善", "恶", "中庸", "狂", "夏", "兽", "花嫁"]


def _try_split_alignment(name: str, name_map: dict[str, int]) -> list[int]:
    """尝试将阵营组合字符串拆解为两个独立特性 ID。

    例如 "秩序善" → [300, 303]，"混沌恶" → [301, 304]。
    如果前缀或后缀不在映射表中，返回空列表。
    """
    for prefix in _ALIGNMENT_AXIS1:
        if name.startswith(prefix):
            suffix = name[len(prefix) :]
            if suffix and prefix in name_map and suffix in name_map:
                return [name_map[prefix], name_map[suffix]]
    return []


def resolve_trait_names(names: list[str]) -> list[int]:
    """将中文特性名列表解析为 trait ID 列表。

    匹配策略：精确匹配 → 阵营组合拆解 → 子串匹配。
    """
    name_map = _ensure_trait_name_map()
    result: list[int] = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        # 标准化分隔符：秩序·善 → 秩序善
        normalized = name.replace("·", "").replace("・", "").replace("‧", "").replace(" ", "")
        # 精确匹配
        if normalized in name_map:
            result.append(name_map[normalized])
            continue
        # 阵营组合拆解：秩序善 → [秩序, 善] → [300, 303]
        alignment_ids = _try_split_alignment(normalized, name_map)
        if alignment_ids:
            result.extend(alignment_ids)
            continue
        # 子串匹配（原始名称）
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
