"""
Laplace — Preset Registry

快捷查询预设组合。每个 Preset 定义一组固定的 Skill 调用模板。
"""

from dataclasses import dataclass, field


@dataclass
class Preset:
    """快捷查询预设。

    Attributes:
        message_as_param: 将用户输入的 message 自动注入到指定 skill 的指定参数中。
            格式: {"skill_name": "param_name"}
            例如: {"lookup_servant": "name"} 表示将 message 填入 lookup_servant 的 name 参数。
            仅当该参数在 param_template 和 user_params 中均未指定时才生效。
    """

    name: str
    display_name: str
    query_skills: list[str]
    response_skill: str = "respond_servant_list"
    param_template: dict[str, dict] = field(default_factory=dict)
    message_as_param: dict[str, str] = field(default_factory=dict)


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
            message_as_param={"lookup_servant": "name"},
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
