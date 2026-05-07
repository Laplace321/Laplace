"""
Query Skill: search_by_cards

按配卡、宝具颜色、宝具目标筛选从者。
迁移自 query_executor.py _filter_cards。
"""

from typing import Literal

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill


class CardsParams(BaseModel):
    """配卡 + 宝具查询参数。"""

    cards: dict[str, int] | None = None
    np_card: Literal["buster", "arts", "quick"] | None = None
    np_target: Literal["one", "all", "support"] | None = None


@register_skill
class SearchByCards(QuerySkill):
    name = "search_by_cards"
    description = "按配卡结构、宝具颜色、宝具目标筛选从者"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return CardsParams

    @property
    def prompt_fragment(self) -> str:
        return (
            "配卡筛选。cards 为指令卡数量要求（如 {'buster': 3}）。"
            "np_card 为宝具颜色：buster/arts/quick。"
            "np_target 为宝具目标：one(单体)/all(全体)/support(辅助)。"
        )

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        return [
            {
                "input": "三红配卡的从者",
                "output": '{"cards": {"buster": 3}, "np_card": null, "np_target": null}',
            },
            {
                "input": "蓝卡配卡的全体宝具从者",
                "output": '{"cards": null, "np_card": "arts", "np_target": "all"}',
            },
        ]

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
