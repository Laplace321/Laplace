"""
Laplace — API and LLM Schemas

Pydantic models for the Skill-Based Architecture routing contract (ADR-018).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.atlas_index import AtlasQueryParams

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

    code: Literal["no_match", "ambiguous", "out_of_scope", "greeting", "atlas_domain", "guide_domain"] = "no_match"
    message: str = ""


class RoutingResponse(BaseModel):
    """Stage 1 LLM 路由的完整输出。"""

    model_config = ConfigDict(extra="ignore")

    skill_calls: list[SkillCall] = Field(default_factory=list, description="要执行的 Skill 调用列表")
    response_skill: str = Field(default="respond_servant_list", description="回复 Skill 名称")
    fallback: FallbackReason | None = Field(default=None, description="降级原因（无匹配时填写）")
    target_pipeline: Literal["A", "B", "C"] | None = Field(
        default=None,
        description="目标链路：A=Skill精确查询, B=Atlas知识问答, C=攻略知识问答。None 表示默认走 A",
    )
    atlas_query: AtlasQueryParams | None = Field(
        default=None,
        description="链路 B 结构化查询参数。仅当 target_pipeline='B' 时填写。从用户问题中提取活动名/从者名/时间/类型等结构化信息。",
    )

    @model_validator(mode="after")
    def validate_atlas_query_required(self) -> "RoutingResponse":
        """当 target_pipeline='B' 时，atlas_query 必须填写。"""
        if self.target_pipeline == "B" and not self.atlas_query:
            raise ValueError("target_pipeline='B' 时 atlas_query 必须填写，不能为 null")
        return self


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

    from server.llm import extract_json_object

    raw = content if isinstance(content, dict) else json.loads(extract_json_object(content))
    try:
        parsed = RoutingResponse.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Routing response validation failed: {e}") from e
    return parsed.model_dump(exclude_none=True)
