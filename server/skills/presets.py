"""
Laplace — Preset Registry

快捷查询预设组合。每个 Preset 定义一组固定的 Skill 调用模板。
"""

from dataclasses import dataclass, field


@dataclass
class Preset:
    """快捷查询预设。"""

    name: str
    display_name: str
    query_skills: list[str]
    response_skill: str = "respond_servant_list"
    param_template: dict[str, dict] = field(default_factory=dict)


PRESET_REGISTRY: dict[str, Preset] = {}


def _register_presets() -> None:
    """注册内置预设。"""
    presets = [
        Preset(
            name="cycle_farming",
            display_name="周回筛选",
            query_skills=["search_by_np_charge"],
            response_skill="respond_servant_list",
            param_template={
                "search_by_np_charge": {"op": "gte", "value": 30},
            },
        ),
        Preset(
            name="servant_lookup",
            display_name="从者查询",
            query_skills=["lookup_servant"],
            response_skill="respond_servant_detail",
        ),
        Preset(
            name="servant_compare",
            display_name="从者对比",
            query_skills=["compare_servants"],
            response_skill="respond_servant_compare",
        ),
        Preset(
            name="support_recommend",
            display_name="辅助推荐",
            query_skills=["search_by_effect"],
            response_skill="respond_support_analysis",
            param_template={
                "search_by_effect": {"effects": ["gainNp"], "effectsOp": "or"},
            },
        ),
    ]
    for p in presets:
        PRESET_REGISTRY[p.name] = p


_register_presets()
