"""Response Skill: 从者对比回复。"""

from server.skills.base import ResponseSkill, register_skill


@register_skill
class RespondServantCompare(ResponseSkill):
    name = "respond_servant_compare"
    description = "对比多个从者并给出分析"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return (
            "你是 FGO 从者查询助手。用户的问题是：\n"
            f"「{user_message}」\n\n"
            f"以下是要对比的从者数据（JSON 格式）：\n{context_json}\n\n"
            "请用中文对这些从者进行对比分析，包括：\n"
            "- 各自优势和劣势\n"
            "- 适用场景差异\n"
            "- 推荐建议\n"
            "使用表格或分点对比，语气友好。"
        )
