"""
Laplace — API and LLM Schemas (Skill-Based Architecture)

Pydantic models for the two-stage LLM routing and Skill execution pipeline.
Stage 1: Routing (skill selection) → Stage 2: Parameter filling → Execution.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# === 共享类型（Query Skills 复用） ===

CompareOp = Literal["eq", "gte", "lte", "gt", "lt"]


class NumericCondition(BaseModel):
    """Numeric comparison condition, e.g. NP charge >= 30."""

    model_config = ConfigDict(extra="ignore")

    op: CompareOp = "eq"
    value: int = 0


# === Stage 1: 路由阶段 Schema ===


class SkillCall(BaseModel):
    """单个 Skill 调用（路由阶段输出）。"""

    model_config = ConfigDict(extra="ignore")

    skill_name: str
    params: dict = Field(default_factory=dict)


class FallbackReason(BaseModel):
    """降级原因（路由阶段第一层兜底）。"""

    model_config = ConfigDict(extra="ignore")

    type: Literal["non_game_query", "unsupported_query", "clarification_needed"]
    message: str = ""


class RoutingResponse(BaseModel):
    """Stage 1 路由输出：选中的 Skills + Response Skill + 可选降级。"""

    model_config = ConfigDict(extra="ignore")

    query_skills: list[SkillCall] = Field(default_factory=list)
    response_skill: str = "respond_servant_list"
    fallback: FallbackReason | None = None


def routing_response_json_schema() -> dict:
    """Return the JSON schema used for OpenAI-compatible response_format."""
    return RoutingResponse.model_json_schema()


# === API 请求/响应 Schema ===


class ChatRequest(BaseModel):
    """对话请求（支持双模式）。"""

    message: str = ""
    mode: Literal["natural_language", "preset"] = "natural_language"
    # preset 模式专用字段
    preset_name: str | None = None
    params: dict[str, dict] | None = None  # {skill_name: params_dict}
    supplement: str | None = None  # 补充描述（B1 策略）
    response_skill: str | None = None

    @field_validator("message", mode="before")
    @classmethod
    def _blank_message_for_preset(cls, value: object, info: object) -> object:
        """preset 模式允许 message 为空。"""
        if isinstance(value, str):
            return value
        return ""


class ChatResponse(BaseModel):
    """对话响应。"""

    reply: str
    servants: list[dict]
    count: int
    query: dict
    model: str
    traceId: str | None = None


# === 向后兼容：保留旧 Schema 供迁移期间使用 ===
# 注意：这些将在 Skill 架构完全稳定后移除


class QueryConditions(BaseModel):
    """[DEPRECATED] Supported servant query filters — 迁移期间保留。"""

    model_config = ConfigDict(extra="ignore")

    npCharge: NumericCondition | None = None
    rarity: NumericCondition | None = None
    className: str | None = None
    name: str | None = None
    names: list[str] | None = None
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
        if isinstance(value, list):
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
    """[DEPRECATED] LLM intent response — 迁移期间保留。"""

    model_config = ConfigDict(extra="ignore")

    intent: Literal["query_servants"]
    conditions: QueryConditions = Field(default_factory=QueryConditions)
    responseTemplate: str | None = None


def intent_response_json_schema() -> dict:
    """[DEPRECATED] Return the JSON schema — 迁移期间保留。"""
    return IntentResponse.model_json_schema()
