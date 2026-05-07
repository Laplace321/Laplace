"""Response Skill: 单从者详细信息回复。"""

from server.skills.base import ResponseSkill, register_skill


@register_skill
class RespondServantDetail(ResponseSkill):
    name = "respond_servant_detail"
    description = "展示单个从者的详细信息"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return (
            "你是 FGO 从者查询助手。用户的问题是：\n"
            f"「{user_message}」\n\n"
            f"以下是该从者的详细数据（JSON 格式）：\n{context_json}\n\n"
            "请用中文详细介绍这位从者的关键信息，包括：\n"
            "- 基本属性（职阶、稀有度、阵营）\n"
            "- 宝具信息（颜色、目标、效果）\n"
            "- 主要技能效果\n"
            "- 配卡信息\n"
            "语气友好自然，适当加入游戏建议。"
        )
