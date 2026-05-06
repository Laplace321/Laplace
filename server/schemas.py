"""
Laplace — API and LLM Schemas

Pydantic models for the structured intent contract between the LLM
and the deterministic query executor.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CompareOp = Literal["eq", "gte", "lte", "gt", "lt"]


class NumericCondition(BaseModel):
    """Numeric comparison condition, e.g. NP charge >= 30."""

    model_config = ConfigDict(extra="ignore")

    op: CompareOp = "eq"
    value: int = 0


class QueryConditions(BaseModel):
    """Supported servant query filters."""

    model_config = ConfigDict(extra="ignore")

    npCharge: NumericCondition | None = None
    rarity: NumericCondition | None = None
    className: str | None = None
    name: str | None = None  # 单从者查询（向后兼容）
    names: list[str] | None = None  # 多从者对比（新增）
    skillEffect: str | None = None
    skillEffects: list[str] | None = None
    skillEffectsOp: Literal["and", "or"] | None = None
    targetType: Literal["self", "party", "enemy"] | None = None
    traits: list[int] | None = None
    excludeTraits: list[int] | None = None
    gender: Literal["male", "female", "unknown"] | None = None
    attribute: Literal["earth", "sky", "human", "star", "beast"] | None = None
    cards: dict[Literal["buster", "arts", "quick"], int] | None = None
    npCard: Literal["buster", "arts", "quick"] | None = None
    npTarget: Literal["one", "all", "support"] | None = None

    @field_validator("className", "name", "skillEffect", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("names", mode="before")
    @classmethod
    def _validate_names(cls, value: object) -> object:
        """确保 names 列表不为空。"""
        if isinstance(value, list):
            # 过滤空字符串
            cleaned = [n.strip() for n in value if isinstance(n, str) and n.strip()]
            return cleaned if cleaned else None
        return value

    @field_validator("skillEffects", "traits", "excludeTraits", mode="before")
    @classmethod
    def _empty_list_to_none(cls, value: object) -> object:
        if value == []:
            return None
        return value

    @field_validator("cards", mode="before")
    @classmethod
    def _empty_dict_to_none(cls, value: object) -> object:
        if value == {}:
            return None
        return value


class IntentResponse(BaseModel):
    """LLM intent response for the first-stage servant query parser."""

    model_config = ConfigDict(extra="ignore")

    intent: Literal["query_servants"]
    conditions: QueryConditions = Field(default_factory=QueryConditions)
    responseTemplate: str | None = None


def intent_response_json_schema() -> dict:
    """Return the JSON schema used for OpenAI-compatible response_format."""
    return IntentResponse.model_json_schema()
