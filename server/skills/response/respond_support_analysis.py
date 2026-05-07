"""Response Skill: 辅助从者推荐分析。"""

from server.skills.base import ResponseSkill, register_skill


@register_skill
class RespondSupportAnalysis(ResponseSkill):
    name = "respond_support_analysis"
    description = "分析辅助从者的能力并推荐搭配"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return (
            "你是 FGO 从者查询助手。用户的问题是：\n"
            f"「{user_message}」\n\n"
            f"以下是候选辅助从者数据（JSON 格式）：\n{context_json}\n\n"
            "请从辅助能力角度分析这些从者，包括：\n"
            "- NP 充能支持能力\n"
            "- 增伤 / Buff 能力\n"
            "- 防御 / 生存支持\n"
            "- 推荐搭配和使用场景\n"
            "按推荐优先级排列，语气友好。"
        )
