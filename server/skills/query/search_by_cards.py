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
        # 配卡
        cards = params.get("cards")
        if cards is not None and isinstance(cards, dict):
            servant_cards = servant.get("cards", {})
            for card_type, count in cards.items():
                if servant_cards.get(card_type, 0) < count:
                    return False

        # 宝具颜色
        np_card = params.get("np_card")
        if np_card is not None:
            if servant.get("npCard", "") != np_card:
                return False

        # 宝具目标
        np_target = params.get("np_target")
        if np_target is not None:
            if servant.get("npTarget", "") != np_target:
                return False

        return True
