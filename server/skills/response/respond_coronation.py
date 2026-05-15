"""
Laplace — Coronation Response Skill

戴冠战专属回复生成 Skill。
根据知识条目/Boss 机制/配队推荐数据生成玩家友好的回复。
"""

from __future__ import annotations

from server.skills.base import ResponseSkill, register_skill

CORONATION_GENERATION_PROMPT = """\
你是 FGO 戴冠战攻略助手。请根据以下检索到的戴冠战数据，回答用户的问题。

## 回复准则

1. **如实回答**：严格基于提供的数据回答，不要脑补或添加数据中没有的信息。
2. **玩家友好语言**：使用 FGO 玩家熟悉的术语，保持自然口语化。
3. **分类展示规则**：
   - 配队推荐：**按角色分类分组展示**，每组开头用一句话说明该分类的定位和作用（使用数据中的 description 字段）。
   - Boss 机制：按阶段（解放战/钻研战/认定战）分别说明，重点标注解法和 tips。
   - 通用知识：直接回答，适当用列表组织信息。
4. **Tier 排列**：从者推荐按 Tier（S > A > B）排列，Tier 高的排在前面。
5. **优缺点并列**：推荐从者时，优点和缺点/注意事项都要提到，帮助玩家做决策。
6. **简洁高效**：不要重复数据中已有的信息，不要加不必要的客套话。
7. **格式规范**：使用 Markdown 列表和粗体突出关键信息。

## 检索结果数据
```json
{context_json}
```

## 用户的问题
{user_query}

请直接输出你的回答。
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
