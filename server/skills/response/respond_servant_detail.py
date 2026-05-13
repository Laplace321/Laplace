"""Response Skill: 单从者详细信息回复。"""

from server.prompts import get_generation_prompt
from server.skills.base import ResponseSkill, register_skill

_DETAIL_SUPPLEMENT = """
【补充指引 — 单从者详情】
本次查询是展示单个从者的详细信息，请在基础规则之上额外注意：
- 详细介绍该从者的关键信息：基本属性（职阶、稀有度、阵营）、宝具信息（颜色、目标、效果）、主要技能效果、配卡信息。
- 配卡字段解读规则：「配卡」是 5 张手牌的构成（蓝卡A/红卡B/绿卡Q），「宝具卡色」与手牌分开计算。展示格式示例：「配卡 QAABB，宝具为蓝卡(Arts)全体」。严禁将宝具卡色混入手牌数量。
- 如果上下文中包含「技能详情」和「宝具详情」，优先使用其中的精确数值进行描述。格式参考："Arts提升 50%（自身，1回合）"、"暴击威力提升 100%（自身，3回合）"。
- 按技能分组介绍效果，每个技能列出其名称和主要效果数值。宝具效果同理。
"""


@register_skill
class RespondServantDetail(ResponseSkill):
    name = "respond_servant_detail"
    description = "展示单个从者的详细信息"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return get_generation_prompt(user_message, context_json) + _DETAIL_SUPPLEMENT
