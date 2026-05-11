"""Skill: 按职阶克制关系筛选从者。

用户说"克制XX职阶"时，查表找出克制该职阶的所有 className，
然后按 className 筛选从者。默认排除 berserker（狂阶克制一切但也被一切克制）。
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

# ── 克制关系缓存 ──
_CLASS_RELATION: dict | None = None
# ── 中文职阶名 → 英文 className 反向映射缓存 ──
_CN_TO_CLASS: dict[str, str] = {}


def _ensure_class_relation() -> dict:
    """懒加载 class_relation.json。"""
    global _CLASS_RELATION
    if _CLASS_RELATION is not None:
        return _CLASS_RELATION
    path = Path(__file__).parent.parent.parent / "knowledge" / "class_relation.json"
    if not path.exists():
        _CLASS_RELATION = {}
        return _CLASS_RELATION
    with open(path, encoding="utf-8") as f:
        _CLASS_RELATION = json.load(f)
    return _CLASS_RELATION


def _ensure_cn_to_class() -> dict[str, str]:
    """懒加载中文职阶名 → 英文 className 反向映射。

    从 config/translations.json 的 className 构建。
    例如 {"剑阶": "saber", "伪装者": "pretender", "Pretender": "pretender", ...}
    """
    if _CN_TO_CLASS:
        return _CN_TO_CLASS
    trans_path = Path(__file__).parent.parent.parent / "config" / "translations.json"
    if not trans_path.exists():
        return _CN_TO_CLASS
    with open(trans_path, encoding="utf-8") as f:
        data = json.load(f)
    class_map = data.get("className", {})
    for eng, cn in class_map.items():
        # 英文名本身也加入映射（如 "saber" → "saber"）
        _CN_TO_CLASS[eng.lower()] = eng.lower()
        # 完整中文名（如 "伪装者(Pretender)"）
        _CN_TO_CLASS[cn] = eng.lower()
        # 去掉括号部分的纯中文（如 "伪装者"）
        if "(" in cn:
            pure_cn = cn.split("(")[0]
            _CN_TO_CLASS[pure_cn] = eng.lower()
            # 括号内的英文也加入（如 "Pretender"）
            eng_in_paren = cn.split("(")[1].rstrip(")")
            _CN_TO_CLASS[eng_in_paren] = eng.lower()
            _CN_TO_CLASS[eng_in_paren.lower()] = eng.lower()
    return _CN_TO_CLASS


def resolve_class_name(cn_name: str) -> str | None:
    """将中文/英文职阶名转换为英文 className。

    支持: "伪装者" / "伪装者(Pretender)" / "Pretender" / "pretender" / "骑阶" 等。
    """
    cn_map = _ensure_cn_to_class()
    # 精确匹配
    if cn_name in cn_map:
        return cn_map[cn_name]
    # 忽略大小写
    lower = cn_name.lower()
    if lower in cn_map:
        return cn_map[lower]
    # 子串匹配（如用户输入"伪装"也能命中"伪装者"）
    for key, val in cn_map.items():
        if cn_name in key or key in cn_name:
            return val
    return None


def get_advantage_classes(target_class: str, include_berserker: bool = False) -> list[str]:
    """查询克制目标职阶的所有 className 列表。

    Args:
        target_class: 目标职阶的英文 className
        include_berserker: 是否包含 berserker

    Returns:
        克制目标职阶的 className 列表
    """
    relation = _ensure_class_relation()
    reverse = relation.get("reverse", {})
    # className 在 Atlas API 中可能是 camelCase（如 alterEgo / moonCancer）
    attackers = reverse.get(target_class, [])
    if not include_berserker:
        attackers = [c for c in attackers if c != "berserker"]
    return attackers


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    target_class: str = Field(alias="targetClass", description="要克制的目标职阶中文名")
    include_berserker: bool = Field(default=False, alias="includeBerserker")


@register_skill
class SearchByClassAdvantage(QuerySkill):
    name = "search_by_class_advantage"
    description = "按职阶克制关系筛选从者（如'克制伪装者的从者'）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        target_cn = params.get("target_class", "")
        include_berserker = params.get("include_berserker", False)

        if not target_cn:
            return True

        # 中文 → 英文
        target_eng = resolve_class_name(target_cn)
        if target_eng is None:
            return True  # 无法识别时不过滤

        # 查克制关系
        advantage_classes = get_advantage_classes(target_eng, include_berserker)
        if not advantage_classes:
            return True  # 无克制数据时不过滤

        servant_class = servant.get("className", "").lower()
        # className 在 DB 中可能是 camelCase，统一小写比较
        return servant_class in [c.lower() for c in advantage_classes]
