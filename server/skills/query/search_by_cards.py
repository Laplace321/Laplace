"""Skill: 按配卡 / 宝具颜色 / 宝具目标筛选从者。"""

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    cards: dict[str, int] | None = Field(default=None, description='配卡要求，如 {"buster": 3}')
    np_card: str | None = Field(default=None, alias="npCard", description="宝具颜色: buster/arts/quick")
    np_target: str | None = Field(default=None, alias="npTarget", description="宝具目标: one/all/support")


@register_skill
class SearchByCards(QuerySkill):
    name = "search_by_cards"
    description = "按配卡组合、宝具颜色、宝具目标筛选从者"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        # 配卡（指令卡分布，与具体宝具无关）
        cards = params.get("cards")
        if cards is not None and isinstance(cards, dict):
            servant_cards = servant.get("cards", {})
            for card_type, count in cards.items():
                if servant_cards.get(card_type, 0) < count:
                    return False

        np_card = params.get("np_card")
        np_target = params.get("np_target")
        if np_card is None and np_target is None:
            return True

        # 双宝具从者（如 BB Dubai、玛修）顶层 npCard/npTarget 仅反映「主宝具」，
        # 辅助宝具的属性会被掩盖。优先遍历 npDetails 数组，任一宝具匹配即视为命中。
        np_details = servant.get("npDetails") or []
        if np_details:
            for np in np_details:
                if np_card is not None and np.get("npCard", "") != np_card:
                    continue
                if np_target is not None and np.get("npTarget", "") != np_target:
                    continue
                return True
            return False

        # 兜底：缺少 npDetails 时回退到顶层字段
        if np_card is not None and servant.get("npCard", "") != np_card:
            return False
        if np_target is not None and servant.get("npTarget", "") != np_target:
            return False
        return True
