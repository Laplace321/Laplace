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


class ClarificationOption(BaseModel):
    """单个确认选项。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="选项标识符，用于回传给后端")
    label: str = Field(description="面向用户的中文选项文本")


class ClarificationRequest(BaseModel):
    """当 LLM 判断参数存在歧义/缺失时输出的确认请求。"""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(description="面向用户的确认问题")
    options: list[ClarificationOption] = Field(description="供用户选择的选项列表")
    ambiguous_field: str = Field(default="", description="歧义参数名称（内部追踪用）")


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
    clarification: ClarificationRequest | None = Field(
        default=None,
        description="当参数存在歧义或缺失时输出确认请求。此时 skill_calls 应为空数组。",
    )

    @model_validator(mode="after")
    def validate_atlas_query_required(self) -> "RoutingResponse":
        """当 target_pipeline='B' 时，atlas_query 必须填写。"""
        if self.target_pipeline == "B" and not self.atlas_query:
            raise ValueError("target_pipeline='B' 时 atlas_query 必须填写，不能为 null")
        return self


# ============================================================
# Stage 0 Classifier Schema（两阶段路由, ADR-024）
# ============================================================


class ClassifierResponse(BaseModel):
    """Stage 0 分类器输出：判断用户查询应走哪条链路。

    Task 4 Batch A 新增 ``turn_type`` 字段：标识本轮相对上一轮的关系。
    分类器尚未学会输出该字段时默认 ``MAJOR``（全新查询，等价于无多轮），
    业务侧 Batch B 接入时会调整 prompt 显式输出。
    """

    model_config = ConfigDict(extra="ignore")

    pipeline: Literal["A", "B", "C", "FALLBACK"] = Field(
        description=(
            "目标链路：A=从者/礼装结构化查询, B=游戏事实知识问答, "
            "C=攻略/评价/主观推荐, FALLBACK=问候/超出范围（直接走预置模板回复）"
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="分类置信度，0.0~1.0。低于阈值时走 fallback",
    )
    turn_type: Literal["MAJOR", "MINOR", "CORRECTION"] = Field(
        default="MAJOR",
        description=(
            "本轮相对上一轮的关系："
            "MAJOR=全新查询（清空多轮状态）；"
            "MINOR=在上一轮结果上追加过滤/切换回复粒度（如「其中弓阶的」「详细说说」）；"
            "CORRECTION=修正上一轮关键参数（如「我说的是Alter版」）。"
            "无多轮上下文时默认为 MAJOR。"
        ),
    )
    fallback_code: Literal["greeting", "out_of_scope"] | None = Field(
        default=None,
        description=(
            "仅当 pipeline=FALLBACK 时使用："
            "greeting=问候/能力咨询（如「你好」「你能做什么」）；"
            "out_of_scope=与 FGO 无关的问题（如「明天天气」「推荐充电器」）。"
            "其他 pipeline 必须保持 null。"
        ),
    )


def classifier_response_json_schema() -> dict:
    """Return the JSON schema for Stage 0 classifier response_format."""
    return ClassifierResponse.model_json_schema()


def parse_classifier_response(content: str | dict) -> dict:
    """Parse and validate a Stage 0 classifier response from LLM.

    可作为 chat_completion 的 response_validator 参数使用。
    """
    import json

    from pydantic import ValidationError

    from server.llm import extract_json_object

    raw = content if isinstance(content, dict) else json.loads(extract_json_object(content))
    try:
        parsed = ClassifierResponse.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Classifier response validation failed: {e}") from e
    return parsed.model_dump()


# ============================================================
# Task 4 Batch B：MINOR/CORRECTION delta 抽取 Schema
# ============================================================


class SkillPatch(BaseModel):
    """对上一轮某个 SkillCall 的参数补丁（CORRECTION 用）。

    例如上一轮 ``lookup_servant({"name": "玛修"})``，本轮用户说「我说的是Alter版」，
    LLM 输出 ``SkillPatch(skill_name="lookup_servant", params={"name": "玛修(Alter)"})``，
    业务侧将 params 浅合并到对应 SkillCall。
    """

    model_config = ConfigDict(extra="ignore")

    skill_name: str = Field(description="要修补的 SkillCall.skill_name（必须在 prev_turn.skill_calls 中存在）")
    params: dict = Field(default_factory=dict, description="要覆盖/补充的参数键值，按 dict.update 合并")


class MinorMergeResponse(BaseModel):
    """MINOR/CORRECTION 多轮合并 LLM 输出。

    LLM 基于 prev_turn.skill_calls + prev_turn.response_skill_name + 本轮 user_message，
    输出一个 ``op`` 决定如何合并：

    - ``reuse``            G1：完全复用 prev skill_calls + response_skill（如「再帮我看看宝具效果」）
    - ``append_filters``   G2：在 prev skill_calls 末尾追加 ``skill_calls``（如「其中弓阶的」）
    - ``switch_response``  G3：保留 prev skill_calls，仅切换 ``response_skill``（如「详细说说」）
    - ``patch_params``     G4 / CORRECTION：对 prev 某个 SkillCall 的参数应用 ``patches``

    多个 op 可同时承载额外字段（如 append_filters 时也可以同时切换 response_skill），
    业务侧按字段是否非空依次应用。
    """

    model_config = ConfigDict(extra="ignore")

    op: Literal["reuse", "append_filters", "switch_response", "patch_params"] = Field(
        description="合并粒度：reuse=完全复用 / append_filters=追加过滤 / switch_response=切换回复 / patch_params=修正参数",
    )
    skill_calls: list[SkillCall] = Field(
        default_factory=list,
        description="追加的 SkillCall（仅 op=append_filters 有效）",
    )
    response_skill: str | None = Field(
        default=None,
        description="新的 response_skill 名称（op=switch_response 必填；其它 op 时为 None 表示保持 prev）",
    )
    patches: list[SkillPatch] = Field(
        default_factory=list,
        description="对 prev SkillCall 的参数补丁（仅 op=patch_params 有效）",
    )
    rationale: str = Field(
        default="",
        description="LLM 简述本轮意图，便于审计；可为空字符串",
    )


def minor_merge_response_json_schema() -> dict:
    """Return the JSON schema for MinorMergeResponse response_format."""
    return MinorMergeResponse.model_json_schema()


def parse_minor_merge_response(content: str | dict) -> dict:
    """Parse and validate a MinorMergeResponse from LLM。

    可作为 chat_completion 的 response_validator 参数使用。
    """
    import json

    from pydantic import ValidationError

    from server.llm import extract_json_object

    raw = content if isinstance(content, dict) else json.loads(extract_json_object(content))
    try:
        parsed = MinorMergeResponse.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"MinorMerge response validation failed: {e}") from e
    return parsed.model_dump(exclude_none=True)


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
