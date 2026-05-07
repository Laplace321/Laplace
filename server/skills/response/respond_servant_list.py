"""Response Skill: 从者列表回复。"""

from server.skills.base import ResponseSkill, register_skill


@register_skill
class RespondServantList(ResponseSkill):
    name = "respond_servant_list"
    description = "以列表形式展示筛选到的从者"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return (
            "你是 FGO 从者查询助手。用户的问题是：\n"
            f"「{user_message}」\n\n"
            f"以下是匹配到的从者数据（JSON 格式）：\n{context_json}\n\n"
            "请用简洁的中文回复，列出从者名称和关键信息。"
            "如果结果较多，总结共同特征并按稀有度分组。"
        )
