"""
Laplace — Preset Skill Combinations

预设组合注册表：提供快捷查询入口，跳过 Stage 1 LLM 路由。
用户选择预设后，表单参数直接实例化为确定的 Skill 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PresetConfig:
    """预设组合配置。"""

    name: str  # 预设标识符（如 cycle_farming）
    display_name: str  # 面向用户的显示名称（如 "周回筛选"）
    description: str  # 简短描述
    query_skills: list[str]  # 包含的 Query Skill 名称列表
    response_skill: str = "respond_servant_list"  # 默认 Response Skill
    param_template: dict = field(default_factory=dict)  # 参数模板/默认值


# 预设组合注册表
PRESET_REGISTRY: dict[str, PresetConfig] = {}


def register_preset(config: PresetConfig) -> PresetConfig:
    """注册一个预设组合。"""
    PRESET_REGISTRY[config.name] = config
    return config


# === 初始预设定义 ===

register_preset(
    PresetConfig(
        name="cycle_farming",
        display_name="周回筛选",
        description="按 NP 充能、职阶、星级筛选适合周回的从者",
        query_skills=[
            "search_by_np_charge",
            "search_by_class",
            "search_by_rarity",
        ],
        response_skill="respond_servant_list",
        param_template={
            "search_by_np_charge": {"op": "gte", "value": 30},
        },
    )
)

register_preset(
    PresetConfig(
        name="servant_compare",
        display_name="从者对比",
        description="对比多个从者的属性和能力",
        query_skills=["compare_servants"],
        response_skill="respond_servant_compare",
        param_template={
            "compare_servants": {"names": []},
        },
    )
)

register_preset(
    PresetConfig(
        name="support_recommend",
        display_name="辅助推荐",
        description="查找辅助型从者（群充、加攻、防御等）",
        query_skills=["search_by_skill_effect"],
        response_skill="respond_support_analysis",
        param_template={
            "search_by_skill_effect": {
                "effects": ["gainNp"],
                "target_type": "party",
            },
        },
    )
)

register_preset(
    PresetConfig(
        name="servant_lookup",
        display_name="从者查询",
        description="按名称查询从者详情",
        query_skills=["lookup_servant"],
        response_skill="respond_servant_detail",
        param_template={
            "lookup_servant": {"name": ""},
        },
    )
)
