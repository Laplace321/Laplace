"""Skill: 按效果搜索概念礼装（满破效果优先，支持数值阈值过滤）。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

# 一般 buff effect 的 effectDetails[].value 使用千分比（如 upBuster value=300 表示 30%），
# 用户传百分比（如 30 表示 ≥30%），乘 10 转换到内部表达。
_CE_DEFAULT_VALUE_MULTIPLIER = 10

# gainNp（初始 NP 充能）使用 svals.Value 标量精度（5000 = 50%、10000 = 100%），
# 与 buff 千分比量纲不同。data_loader 已抽出预算好的 npChargePercent 字段（int 百分比），
# 直接用它做阈值匹配最简洁。
_NP_CHARGE_EFFECTS = {"gainNp"}


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    effect: str = Field(description="效果名（如 gainNp, upBuster, invincible 等）")
    limit_break: bool = Field(default=True, description="是否搜索满破效果（默认 True）")
    min_value: int | None = Field(
        default=None,
        alias="minValue",
        description="效果最小数值（百分比，如 50 表示 ≥50%）",
    )
    max_value: int | None = Field(
        default=None,
        alias="maxValue",
        description="效果最大数值（百分比）",
    )


def _match_buff_value(
    details: list[dict],
    effect_name: str,
    min_val: int | None,
    max_val: int | None,
) -> bool:
    """检查 effectDetails 中是否存在符合千分比阈值的目标效果条目。"""
    for detail in details:
        if detail.get("name") != effect_name:
            continue
        value = detail.get("value")
        if value is None:
            # 无数值字段（如 invincible 等纯状态效果），无法按阈值匹配，跳过
            continue
        if min_val is not None and value < min_val:
            continue
        if max_val is not None and value > max_val:
            continue
        return True
    return False


@register_skill
class CESearchByEffect(QuerySkill):
    name = "ce_search_by_effect"
    description = "按效果搜索概念礼装（默认搜满破效果，支持 minValue/maxValue 数值过滤）"
    domain = "ce"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, ce: dict, params: dict) -> bool:
        effect_name = params.get("effect", "")
        limit_break = params.get("limit_break", True)
        raw_min = params.get("min_value")
        raw_max = params.get("max_value")

        if not effect_name:
            return False

        # 选择满破或未满破效果列表
        if limit_break:
            effects = ce.get("effectsLimitBreak", [])
            details = ce.get("effectDetailsLB", [])
        else:
            effects = ce.get("effects", [])
            details = ce.get("effectDetails", [])

        # 必须先命中 effect 名
        if effect_name not in effects:
            return False

        # 无数值条件：直接命中
        if raw_min is None and raw_max is None:
            return True

        # gainNp：用预算好的 npChargePercent（百分比 int）做阈值匹配
        if effect_name in _NP_CHARGE_EFFECTS:
            np_pct = ce.get("npChargePercent")
            if np_pct is None:
                return False
            if raw_min is not None and np_pct < raw_min:
                return False
            if raw_max is not None and np_pct > raw_max:
                return False
            return True

        # 其他 buff effect：用户百分比 → 内部千分比
        min_val = raw_min * _CE_DEFAULT_VALUE_MULTIPLIER if raw_min is not None else None
        max_val = raw_max * _CE_DEFAULT_VALUE_MULTIPLIER if raw_max is not None else None
        return _match_buff_value(details, effect_name, min_val, max_val)
