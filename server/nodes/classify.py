"""Pipeline A 入口节点 — Stage 0 链路分类器（ADR-024 / ADR-028）。

迁移自 ``server/pipeline.py`` 的 Stage 0 分类逻辑。本节点行为与原代码完全等价：
- 调用 ``build_classifier_prompt`` 提示词，最多 2 次 LLM 重试
- 把分类结果写回 state.classified_pipeline / classifier_confidence / classifier_model
- 累计 trace_total_tokens

降级行为：
- 2 次重试均失败 → 视为 A 链路 + 1.0 置信度（兼容旧逻辑），由 ``edges.after_classify`` 决定后续路径
"""

from __future__ import annotations

from server.graph.state import PipelineState
from server.llm import chat_completion
from server.logger import log_trace_event
from server.prompts import build_classifier_prompt
from server.schemas import classifier_response_json_schema, parse_classifier_response


async def classify_node(state: PipelineState) -> PipelineState:
    """Stage 0：链路分类（A=Skill 查询 / B=Atlas 知识 / C=攻略文档）。

    输入：state.user_message / state.trace_id
    输出：state.classified_pipeline / state.classifier_confidence / state.classifier_model /
          state.trace_total_tokens
    """
    classifier_prompt = build_classifier_prompt()
    classifier_result = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            classifier_result = await chat_completion(
                system_prompt=classifier_prompt,
                user_message=state.user_message,
                temperature=0.0,
                json_mode=True,
                response_schema=classifier_response_json_schema,
                response_validator=parse_classifier_response,
            )
            break
        except Exception as cls_err:  # noqa: BLE001
            last_error = cls_err
            if attempt == 0:
                print(f"⚠️ [{state.trace_id}] Stage 0 分类第 1 次尝试失败，重试中: {cls_err}")

    # Stage 0 失败 → 降级走 A 全量路由（兼容旧逻辑：高置信度 1.0 让后续 route_node 处理）
    if classifier_result is None:
        print(f"⚠️ [{state.trace_id}] Stage 0 分类 2 次均失败，降级全量路由: {last_error}")
        state.classified_pipeline = "A"
        state.classifier_confidence = 1.0
        state.classifier_model = "unknown"
        return state

    classifier_model = classifier_result.pop("_model", "unknown")
    classifier_result.pop("_response_format", None)
    classifier_result.pop("_provider", None)
    classifier_result.pop("_attempts", None)
    classifier_usage = classifier_result.pop("_usage", {})
    state.trace_total_tokens += classifier_usage.get("total_tokens", 0)
    state.classified_pipeline = classifier_result.get("pipeline", "A")
    state.classifier_confidence = classifier_result.get("confidence", 0.0)
    state.classifier_model = classifier_model

    await log_trace_event(
        state.trace_id,
        "classifier_output",
        {
            "pipeline": state.classified_pipeline,
            "confidence": state.classifier_confidence,
            "model": classifier_model,
            "usage": classifier_usage,
        },
    )

    return state
