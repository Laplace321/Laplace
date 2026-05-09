"""Response Skill: 从者对比回复。"""

from server.prompts import get_generation_prompt
from server.skills.base import ResponseSkill, register_skill

_COMPARE_SUPPLEMENT = """
【补充指引 — 从者对比】
本次查询是对比多个从者，请在基础规则之上额外注意：
- 对这些从者进行对比分析：各自优势、适用场景差异。
- 优先使用表格或分点对比，语气友好。
"""


@register_skill
class RespondServantCompare(ResponseSkill):
    name = "respond_servant_compare"
    description = "对比多个从者并给出分析"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return get_generation_prompt(user_message, context_json) + _COMPARE_SUPPLEMENT
