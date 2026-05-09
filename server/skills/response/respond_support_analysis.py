"""Response Skill: 辅助从者推荐分析。"""

from server.prompts import get_generation_prompt
from server.skills.base import ResponseSkill, register_skill

_SUPPORT_SUPPLEMENT = """
【补充指引 — 辅助分析】
本次查询是分析辅助从者的能力，请在基础规则之上额外注意：
- 从辅助能力角度分析：NP 充能支持、增伤/Buff 能力、防御/生存支持。
- 按推荐优先级排列，语气友好。
"""


@register_skill
class RespondSupportAnalysis(ResponseSkill):
    name = "respond_support_analysis"
    description = "分析辅助从者的能力并推荐搭配"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return get_generation_prompt(user_message, context_json) + _SUPPORT_SUPPLEMENT
