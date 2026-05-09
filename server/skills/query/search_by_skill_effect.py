"""Skill: 按技能效果筛选从者。"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect
from server.skills.base import QuerySkill, register_skill

# ── 中文→英文效果名反查表（从 effect_schema.json 加载）──
_ZH_TO_EN: dict[str, str] = {}


def _ensure_zh_map() -> dict[str, str]:
    """懒加载中文→英文效果名映射。"""
    if _ZH_TO_EN:
        return _ZH_TO_EN
    schema_path = Path(__file__).parent.parent.parent / "knowledge" / "effect_schema.json"
    if not schema_path.exists():
        return _ZH_TO_EN
    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)
    for effect in data.get("effects", []):
        name = effect["name"]
        for alias in effect.get("aliases_zh", []):
            _ZH_TO_EN[alias] = name
    return _ZH_TO_EN


def _resolve_effect_name(name: str) -> str:
    """将可能的中文效果名解析为英文 key，已是英文则原样返回。

    匹配策略：
    1. 精确匹配中文别名表
    2. 子串模糊 fallback（如 "攻击提升" 子串匹配 "攻击力提升"）
    """
    zh_map = _ensure_zh_map()
    # 精确匹配
    if name in zh_map:
        return zh_map[name]
    # 子串模糊 fallback：name 是某个别名的子串，或某个别名是 name 的子串
    for alias, en_name in zh_map.items():
        if name in alias or alias in name:
            return en_name
    return name


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str | None = Field(default=None, alias="skillEffect", description="单效果名")
    effects: list[str] | None = Field(default=None, alias="skillEffects", description="多效果列表")
    effects_op: str = Field(default="and", alias="skillEffectsOp", description="多效果组合: and/or")
    target_type: str | None = Field(default=None, alias="targetType", description="目标类型: self/party/enemy")


@register_skill
class SearchBySkillEffect(QuerySkill):
    name = "search_by_skill_effect"
    description = "按技能效果筛选从者（如无敌、回血、NP 充能等）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        effect = params.get("effect")
        effects = params.get("effects")
        target_type = params.get("target_type")

        # 单效果模式
        if effect is not None:
            effect = _resolve_effect_name(effect)
            return _match_effect(servant, effect, target_type)

        # 多效果模式
        if effects is not None and isinstance(effects, list):
            resolved = [_resolve_effect_name(eff) for eff in effects]
            op = params.get("effects_op", "and").lower()
            if op == "or":
                return any(_match_effect(servant, eff, target_type) for eff in resolved)
            else:
                return all(_match_effect(servant, eff, target_type) for eff in resolved)

        return True
