"""Skill: 按技能效果筛选从者。"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _match_effect
from server.skills.base import QuerySkill, register_skill

# ── 中文→英文效果名反查表（从 effect_schema.json 加载）──
_ZH_TO_EN: dict[str, str] = {}
# ── 复合效果展开表：composite_name → [子效果列表] ──
_COMPOSITE_MAP: dict[str, list[str]] = {}


def _ensure_zh_map() -> dict[str, str]:
    """懒加载中文→英文效果名映射，同时构建复合效果展开表。"""
    if _ZH_TO_EN:
        return _ZH_TO_EN
    schema_path = Path(__file__).parent.parent.parent / "knowledge" / "effect_schema.json"
    if not schema_path.exists():
        return _ZH_TO_EN
    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)
    from server.data_loader import merge_effect_overlay

    all_effects = merge_effect_overlay(data.get("effects", []))
    for effect in all_effects:
        name = effect["name"]
        # 记录复合效果展开关系
        if effect.get("composite"):
            _COMPOSITE_MAP[name] = effect.get("includes", [])
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


def _expand_effect(name: str) -> list[str]:
    """如果是复合效果则展开为子效果列表，否则返回单元素列表。

    支持中文/英文输入，先 resolve 再查复合表。
    """
    _ensure_zh_map()
    resolved = _resolve_effect_name(name)
    if resolved in _COMPOSITE_MAP:
        return _COMPOSITE_MAP[resolved]
    return [resolved]


# ── CD 联合匹配相关常量与工具函数 ──
# 效果类型→数值基数映射（参考 Chaldea const_data.dart）
_PERCENT_BASE_100_EFFECTS = frozenset({"gainNp", "regainNp"})
_NO_PERCENT_EFFECTS = frozenset({"gainStar"})


def _convert_value(
    effect_name: str,
    raw_min: int | None,
    raw_max: int | None,
) -> tuple[int | None, int | None]:
    """将用户传入的百分比数值转换为内部 Value 单位。

    - gainNp/regainNp: base=100 → 用户传50 × 100 = 5000
    - gainStar: 整数值 → 不转换（multiplier=1）
    - Buff 类（upAtk/upBuster 等）: base=10 → 用户传50 × 10 = 500
    """
    if raw_min is None and raw_max is None:
        return None, None

    if effect_name in _PERCENT_BASE_100_EFFECTS:
        multiplier = 100
    elif effect_name in _NO_PERCENT_EFFECTS:
        multiplier = 1
    else:
        multiplier = 10

    min_value = raw_min * multiplier if raw_min is not None else None
    max_value = raw_max * multiplier if raw_max is not None else None
    return min_value, max_value


def _match_skill_with_cd(
    servant: dict,
    effect_name: str | None,
    target_type: str | None,
    min_value: int | None,
    max_value: int | None,
    max_cd: int,
) -> bool:
    """单技能粒度联合匹配：在同一个技能内同时检查效果+数值+CD。

    当 effect_name 为 None 时（纯 CD 查询），只检查技能 CD ≤ max_cd。
    """
    for skill in servant.get("skillDetails", []):
        cool_down = skill.get("coolDown", 0)
        if cool_down <= 0 or cool_down > max_cd:
            continue
        # 纯 CD 查询
        if effect_name is None:
            return True
        # 效果匹配：遍历该技能内的效果
        for eff in skill.get("effects", []):
            if eff.get("type") != effect_name:
                continue
            # 目标类型检查
            if target_type and eff.get("targetType") != target_type:
                continue
            # 数值检查
            value = eff.get("valueMax")
            if value is not None:
                if min_value is not None and value < min_value:
                    continue
                if max_value is not None and value > max_value:
                    continue
            elif min_value is not None:
                continue
            return True
    return False


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str | None = Field(default=None, alias="skillEffect", description="单效果名")
    effects: list[str] | None = Field(default=None, alias="skillEffects", description="多效果列表")
    effects_op: str = Field(default="and", alias="skillEffectsOp", description="多效果组合: and/or")
    target_type: str | None = Field(
        default=None, alias="targetType", description="目标类型: self/party(含单体队友)/ptOne(仅单体队友)/enemy"
    )
    min_value: int | None = Field(default=None, alias="minValue", description="效果最小数值（百分比，如50表示≥50%）")
    max_value: int | None = Field(default=None, alias="maxValue", description="效果最大数值（百分比）")
    max_cd: int | None = Field(default=None, alias="maxCd", description="技能CD上限（回合数），同一技能内联合校验")


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
        raw_min = params.get("min_value")
        raw_max = params.get("max_value")
        max_cd = params.get("max_cd")

        # ── CD 联合匹配路径（同一技能粒度内） ──
        # 当 max_cd 有值时，使用 _match_skill_with_cd 保证同一技能内同时满足效果+数值+CD
        if max_cd is not None:
            if effect is not None:
                expanded = _expand_effect(effect)
                for sub_eff in expanded:
                    sub_min, sub_max = _convert_value(sub_eff, raw_min, raw_max)
                    if _match_skill_with_cd(servant, sub_eff, target_type, sub_min, sub_max, max_cd):
                        return True
                return False
            # 纯 CD 查询（effect=None）：只检查是否有任一技能 CD ≤ max_cd
            return _match_skill_with_cd(servant, None, None, None, None, max_cd)

        # ── 标准效果匹配路径（无 CD 约束，行为不变） ──
        # 百分比 → 千分比转换（LLM 传 50 表示 50%，内部用 500‰）
        min_value = raw_min * 10 if raw_min is not None else None
        max_value = raw_max * 10 if raw_max is not None else None

        # 单效果模式（支持复合效果自动展开为 OR）
        if effect is not None:
            expanded = _expand_effect(effect)
            if len(expanded) > 1:
                return any(_match_effect(servant, eff, target_type, min_value, max_value) for eff in expanded)
            return _match_effect(servant, expanded[0], target_type, min_value, max_value)

        # 多效果模式
        if effects is not None and isinstance(effects, list):
            resolved = [_resolve_effect_name(eff) for eff in effects]
            op = params.get("effects_op", "and").lower()
            if op == "or":
                return any(_match_effect(servant, eff, target_type, min_value, max_value) for eff in resolved)
            else:
                return all(_match_effect(servant, eff, target_type, min_value, max_value) for eff in resolved)

        return True
