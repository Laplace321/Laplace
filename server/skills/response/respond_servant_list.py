"""Response Skill: 从者列表回复。"""

from server.prompts import get_generation_prompt
from server.skills.base import ResponseSkill, register_skill


@register_skill
class RespondServantList(ResponseSkill):
    name = "respond_servant_list"
    description = "以列表形式展示筛选到的从者"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return get_generation_prompt(user_message, context_json)
