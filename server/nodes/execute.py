"""Pipeline A 执行节点 — Skill 执行（SkillExecutor）。

迁移自 ``server/pipeline.py`` 的执行段（``executor.execute(...)`` 起）。本节点行为与原代码等价：
- 调用 ``SkillExecutor.execute(skill_calls, response_skill_name)``
- 主路径：写 ``state.extras["executor_result"]`` 供 generate_node 使用，更新 servants/count
- 降级条件 → state.extras["bail_out"] = <reason>：
  - "execution_clarification"  result.clarification 非空（含 guess_candidates_async 后仍存在的情况）
  - "execution_fallback"       result.is_fallback（含 try_resolve_nickname_async 后仍存在的情况）
"""

from __future__ import annotations

from server.context_builder import MAX_RESULTS
from server.graph.decorators import with_trace
from server.graph.state import PipelineState
from server.logger import Phase, log_trace_event
from server.skills.executor import SkillExecutor


@with_trace(Phase.NODE_EXECUTE)
async def execute_node(state: PipelineState) -> PipelineState:
    """Skill 执行：调度 QuerySkill → 收敛 servants/total_found。"""
    streaming = bool(state.extras.get("streaming"))
    if streaming:
        # 判断是否包含知识类 Skill（用于动态调整 thinking message）
        from server.skills.base import SKILL_REGISTRY

        has_knowledge_skill = (
            any(
                getattr(SKILL_REGISTRY.get(c.get("skill_name", "")), "domain", "servant") != "servant"
                for c in state.skill_calls
            )
            if state.skill_calls
            else False
        )
        executing_msg = "正在检索知识库..." if has_knowledge_skill else "正在检索从者数据..."
        state.pending_events.append({"type": "thinking", "data": {"phase": "executing", "message": executing_msg}})

    executor = SkillExecutor()
    result = executor.execute(state.skill_calls, state.response_skill_name)

    # BI 维度回填：accepted skills（按字典序去重拼接，控制基数）
    if result.accepted_skills:
        skill_name_set = {
            s.get("skill_name", "") for s in result.accepted_skills if isinstance(s, dict) and s.get("skill_name")
        }
        if skill_name_set:
            state.metric_labels["skill_names"] = ",".join(sorted(skill_name_set))

    # ── Trace: execution ──
    await log_trace_event(
        state.trace_id,
        "execution",
        {
            "accepted_skills": result.accepted_skills,
            "rejected_skills": result.rejected_skills,
            "total_found": result.total_found,
            "execution_time_ms": round(result.execution_time_ms, 2),
            "is_fallback": result.is_fallback,
            "has_clarification": result.clarification is not None,
        },
    )

    # ── 执行层 clarification 检测 ──
    if result.clarification:
        from server.skills.executor import CLARIFICATION_EMPTY_NAME

        # 名称查询空结果：异步 LLM 猜测填充候选
        if result.clarification.get("type") == CLARIFICATION_EMPTY_NAME:
            if streaming:
                state.pending_events.append(
                    {"type": "thinking", "data": {"phase": "resolving", "message": "正在智能识别..."}}
                )
            result = await executor.guess_candidates_async(result)

        # 猜测后仍有 clarification → bail_out
        if result.clarification:
            state.extras["bail_out"] = "execution_clarification"
            state.extras["executor_result"] = result
            return state

    # 执行阶段 fallback：先尝试异步昵称识别
    if result.is_fallback:
        result = await executor.try_resolve_nickname_async(result, state.skill_calls)
        if not result.is_fallback:
            # 昵称识别成功，更新 trace
            await log_trace_event(
                state.trace_id,
                "execution_resolve_nickname",
                {
                    "accepted_skills": result.accepted_skills,
                    "total_found": result.total_found,
                    "execution_time_ms": round(result.execution_time_ms, 2),
                },
            )

    if result.is_fallback:
        state.extras["bail_out"] = "execution_fallback"
        state.extras["executor_result"] = result
        return state

    # 主路径成功：保存 executor_result + 卡片数据
    state.extras["executor_result"] = result
    state.servants = result.servants[:MAX_RESULTS]
    state.count = result.total_found
    return state
