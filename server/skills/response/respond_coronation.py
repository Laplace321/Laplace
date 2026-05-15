"""
Laplace — Coronation Response Skill

戴冠战专属回复生成 Skill。
根据知识条目/Boss 机制/配队推荐数据生成玩家友好的回复。
"""

from __future__ import annotations

from server.skills.base import ResponseSkill, register_skill

CORONATION_GENERATION_PROMPT = """\
你是 FGO 戴冠战攻略助手。严格基于以下数据回答，禁止脑补。

## 准则
1. **严禁引用数据中不存在的从者或信息**。只推荐数据里列出的从者，不可自行添加。
2. 配队推荐按角色分类分组，每组简述定位，从者按 Tier(S>A>B) 排列，优缺点并列。
3. Boss 机制按阶段说明，标注解法和 tips。通用知识直接回答，用列表组织。
4. 使用 FGO 玩家术语，Markdown 粗体突出关键信息，简洁高效。

## 数据
```json
{context_json}
```

## 问题
{user_query}
"""


@register_skill
class RespondCoronationSkill(ResponseSkill):
    """戴冠战专属回复生成。"""

    name = "respond_coronation"
    description = "生成戴冠战相关查询的回复（知识/Boss/配队）"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        """构建戴冠战专属 Generation Prompt。"""
        return CORONATION_GENERATION_PROMPT.format(
            context_json=context_json,
            user_query=user_message,
        )
