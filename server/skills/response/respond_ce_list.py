"""Response Skill: 概念礼装列表回复。"""

from server.skills.base import ResponseSkill, register_skill

_CE_GENERATION_PROMPT = """你是 FGO 概念礼装查询助手。根据用户问题和筛选结果，用自然语言回复用户。

## 回复规范
1. 用中文回复，语气自然友好
2. 优先展示满破效果，NP 充能数值高亮说明
3. 对每个礼装简要说明其核心效果和适用场景
4. 如果结果很多（>10），只介绍前几个代表性礼装并说明总数
5. 严禁出现任何英文技术术语、字段名或系统内部名称
6. 使用玩家熟悉的表述方式（如"纯攻型"而非"pure_atk"）

## 用户问题
{user_message}

## 筛选结果数据
{context_json}

请根据以上信息回复用户。"""


@register_skill
class RespondCEList(ResponseSkill):
    name = "respond_ce_list"
    description = "以列表形式展示筛选到的概念礼装"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return _CE_GENERATION_PROMPT.format(
            user_message=user_message,
            context_json=context_json,
        )
