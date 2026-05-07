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
    npEffect: str | None = None
    npEffects: list[str] | None = None
    npEffectsOp: Literal["and", "or"] | None = None
    targetType: Literal["self", "party", "enemy"] | None = None
    traits: list[int] | None = None
    excludeTraits: list[int] | None = None
    gender: Literal["male", "female", "unknown"] | None = None
    attribute: Literal["earth", "sky", "human", "star", "beast"] | None = None
    cards: dict[Literal["buster", "arts", "quick"], int] | None = None
    npCard: Literal["buster", "arts", "quick"] | None = None
    npTarget: Literal["one", "all", "support"] | None = None

    @field_validator("className", "name", "skillEffect", "npEffect", mode="before")
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

    @field_validator("skillEffects", "npEffects", "traits", "excludeTraits", mode="before")
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


# ============================================================
# Stage 1 Routing Schema（Skill-Based Architecture, ADR-018）
# ============================================================


class SkillCall(BaseModel):
    """Stage 1 路由输出的单个 Skill 调用。"""

    model_config = ConfigDict(extra="ignore")

    skill_name: str = Field(description="要调用的 Skill 名称")
    params: dict = Field(default_factory=dict, description="Skill 参数")


class FallbackReason(BaseModel):
    """当路由无法匹配任何 Skill 时的降级原因。"""

    model_config = ConfigDict(extra="ignore")

    code: Literal["no_match", "ambiguous", "out_of_scope"] = "no_match"
    message: str = ""


class RoutingResponse(BaseModel):
    """Stage 1 LLM 路由的完整输出。"""

    model_config = ConfigDict(extra="ignore")

    skill_calls: list[SkillCall] = Field(default_factory=list, description="要执行的 Skill 调用列表")
    response_skill: str = Field(default="respond_servant_list", description="回复 Skill 名称")
    fallback: FallbackReason | None = Field(default=None, description="降级原因（无匹配时填写）")


def routing_response_json_schema() -> dict:
    """Return the JSON schema for Stage 1 routing response_format."""
    return RoutingResponse.model_json_schema()


def parse_routing_response(content: str | dict) -> dict:
    """Parse and validate a Stage 1 routing response from LLM.

    与 parse_intent_response 类似，但校验 RoutingResponse 模型。
    可作为 chat_completion 的 response_validator 参数使用。
    """
    import json

    from pydantic import ValidationError

    from server.llm_client import extract_json_object

    raw = content if isinstance(content, dict) else json.loads(extract_json_object(content))
    try:
        parsed = RoutingResponse.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Routing response validation failed: {e}") from e
    return parsed.model_dump(exclude_none=True)
