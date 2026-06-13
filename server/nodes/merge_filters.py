"""Pipeline A 多轮合并节点 — MINOR / CORRECTION 粒度合并（ADR-028 / Task 4 Batch B）。

本节点仅在以下条件全部满足时被图引擎调度：
- ``state.turn_type`` ∈ {``MINOR``, ``CORRECTION``}
- ``state.extras["prev_turn"]`` 存在（由 classify_node 填入）

行为：调用 LLM 抽取 delta 操作（``MinorMergeResponse``），按 op 应用到 state.skill_calls /
state.response_skill_name，跳过 route_node 直接进入 execute_node。

四种粒度（与 plan T4.7 对齐）：
- **G1 reuse**：完全复用 prev skill_calls + response_skill（如「再帮我看看宝具效果」）
- **G2 append_filters**：在 prev skill_calls 末尾追加新 SkillCall（如「其中弓阶的」追加 search_by_class）
- **G3 switch_response**：保留 prev skill_calls，仅切换 response_skill（如「详细说说」→ respond_servant_detail）
- **G4 patch_params**：CORRECTION 时对 prev SkillCall 的参数应用补丁（如「我说的是Alter版」修正 lookup_servant.name）

降级策略（plan §风险点）：
LLM 调用 / 解析 / 校验任一失败 → 视为 MAJOR 处理：
- 清空 prev_turn / 重置 turn_type=MAJOR
- 不写 skill_calls / response_skill_name（保留默认空值）
- 在 state.extras 中写入 ``bail_out="merge_failed_fallback_route"`` 由 edges 决定走 route_node 重新路由
"""

from __future__ import annotations

import copy

from server.graph.decorators import with_trace
from server.graph.session import TurnSnapshot
from server.graph.state import PipelineState
from server.llm import chat_completion
from server.logger import Phase, log_trace_event
from server.prompts import build_minor_merge_prompt
from server.schemas import (
    minor_merge_response_json_schema,
    parse_minor_merge_response,
)


@with_trace(Phase.NODE_MERGE_FILTERS)
async def merge_filters_node(state: PipelineState) -> PipelineState:
    """MINOR/CORRECTION 多轮合并节点。

    输入：state.user_message / state.turn_type / state.extras["prev_turn"]
    输出：state.skill_calls / state.response_skill_name / state.target_pipeline = "A"
          失败时：state.turn_type="MAJOR" + state.extras["bail_out"]="merge_failed_fallback_route"
    """
    prev_turn: TurnSnapshot | None = state.extras.get("prev_turn")
    # 调用前置防御：缺 prev_turn 或 turn_type 不合法时直接降级为 MAJOR
    if prev_turn is None or state.turn_type not in ("MINOR", "CORRECTION"):
        state.turn_type = "MAJOR"
        state.extras.pop("prev_turn", None)
        state.extras["bail_out"] = "merge_failed_fallback_route"
        await log_trace_event(
            state.trace_id,
            "minor_merge_skipped",
            {
                "reason": "no_prev_turn_or_invalid_turn_type",
                "turn_type": state.turn_type,
            },
        )
        return state

    prev_skill_calls = list(prev_turn.skill_calls or [])
    prev_response_skill = prev_turn.response_skill_name or "respond_servant_list"
    prev_summary = prev_turn.truncated_summary()

    merge_prompt = build_minor_merge_prompt(
        user_message=state.user_message,
        turn_type=state.turn_type,
        prev_skill_calls=prev_skill_calls,
        prev_response_skill=prev_response_skill,
        prev_summary=prev_summary,
    )

    merge_result = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            merge_result = await chat_completion(
                system_prompt=merge_prompt,
                user_message=state.user_message,
                temperature=0.0,
                json_mode=True,
                response_schema=minor_merge_response_json_schema,
                response_validator=parse_minor_merge_response,
            )
            break
        except Exception as merge_err:  # noqa: BLE001
            last_error = merge_err
            if attempt == 0:
                print(f"⚠️ [{state.trace_id}] MINOR merge 第 1 次尝试失败，重试: {merge_err}")

    # LLM 2 次均失败 → 降级为 MAJOR 重新走路由
    if merge_result is None:
        print(f"⚠️ [{state.trace_id}] MINOR merge 2 次均失败，降级走 route_node: {last_error}")
        state.turn_type = "MAJOR"
        state.extras.pop("prev_turn", None)
        state.extras["bail_out"] = "merge_failed_fallback_route"
        await log_trace_event(
            state.trace_id,
            "minor_merge_failed",
            {"error": str(last_error)},
        )
        return state

    merge_model = merge_result.pop("_model", "unknown")
    merge_result.pop("_response_format", None)
    merge_result.pop("_provider", None)
    merge_result.pop("_attempts", None)
    merge_usage = merge_result.pop("_usage", {})
    state.trace_total_tokens += merge_usage.get("total_tokens", 0)

    op = merge_result.get("op", "")
    extra_calls = merge_result.get("skill_calls", []) or []
    new_response_skill = merge_result.get("response_skill")
    patches = merge_result.get("patches", []) or []

    # ── 应用 delta：始终从 prev 的深拷贝开始，避免改写 SessionStore 中的 frozen snapshot ──
    merged_calls: list[dict] = copy.deepcopy(prev_skill_calls)
    final_response_skill = prev_response_skill

    if op == "reuse":
        # G1：完全复用，不做任何修改
        pass
    elif op == "append_filters":
        # G2：去重追加（按 (skill_name, params dict 排序键) 简单去重，避免 LLM 重复 prev 调用）
        existing_keys = {(c.get("skill_name", ""), _params_key(c.get("params", {}))) for c in merged_calls}
        for call in extra_calls:
            key = (call.get("skill_name", ""), _params_key(call.get("params", {})))
            if key not in existing_keys:
                merged_calls.append({"skill_name": call.get("skill_name", ""), "params": call.get("params", {})})
                existing_keys.add(key)
        if new_response_skill:
            final_response_skill = new_response_skill
    elif op == "switch_response":
        if not new_response_skill:
            # 协议要求必填，缺失则降级
            print(f"⚠️ [{state.trace_id}] MINOR merge op=switch_response 缺 response_skill，降级")
            state.turn_type = "MAJOR"
            state.extras.pop("prev_turn", None)
            state.extras["bail_out"] = "merge_failed_fallback_route"
            await log_trace_event(
                state.trace_id,
                "minor_merge_failed",
                {"reason": "switch_response_missing_skill"},
            )
            return state
        final_response_skill = new_response_skill
    elif op == "patch_params":
        # G4：对 prev 中匹配的 SkillCall 浅合并 params；skill_name 不存在则跳过该补丁
        prev_index: dict[str, list[int]] = {}
        for idx, call in enumerate(merged_calls):
            prev_index.setdefault(call.get("skill_name", ""), []).append(idx)
        applied_patches: list[dict] = []
        for patch in patches:
            target_name = patch.get("skill_name", "")
            target_params = patch.get("params", {}) or {}
            indices = prev_index.get(target_name, [])
            if not indices or not target_params:
                continue
            for idx in indices:
                merged_calls[idx]["params"].update(target_params)
            applied_patches.append({"skill_name": target_name, "params": target_params})
        if not applied_patches:
            # 一个 patch 都没命中，说明 LLM 输出脏数据，降级为 MAJOR
            print(f"⚠️ [{state.trace_id}] MINOR merge op=patch_params 无有效补丁，降级")
            state.turn_type = "MAJOR"
            state.extras.pop("prev_turn", None)
            state.extras["bail_out"] = "merge_failed_fallback_route"
            await log_trace_event(
                state.trace_id,
                "minor_merge_failed",
                {"reason": "patch_params_no_valid_patches", "raw_patches": patches},
            )
            return state
        if new_response_skill:
            final_response_skill = new_response_skill
    else:
        # 未知 op（schema 已限制为 4 种，但防御性兜底）
        print(f"⚠️ [{state.trace_id}] MINOR merge 未知 op={op!r}，降级")
        state.turn_type = "MAJOR"
        state.extras.pop("prev_turn", None)
        state.extras["bail_out"] = "merge_failed_fallback_route"
        await log_trace_event(
            state.trace_id,
            "minor_merge_failed",
            {"reason": "unknown_op", "op": op},
        )
        return state

    # ── 写回 state（A 链路 execute_node 所需字段）──
    state.skill_calls = merged_calls
    state.response_skill_name = final_response_skill
    state.target_pipeline = "A"
    # 与 route_node 行为对齐：清空 routing_result / bail_out，避免 edges 误判
    state.extras.pop("bail_out", None)
    state.extras["routing_result"] = {
        "skill_calls": merged_calls,
        "response_skill": final_response_skill,
        "target_pipeline": "A",
        "source": "minor_merge",
    }

    await log_trace_event(
        state.trace_id,
        "minor_merge_output",
        {
            "op": op,
            "turn_type": state.turn_type,
            "merged_skill_calls": merged_calls,
            "response_skill": final_response_skill,
            "rationale": merge_result.get("rationale", ""),
            "model": merge_model,
            "usage": merge_usage,
        },
    )
    return state


def _params_key(params: dict) -> str:
    """把 dict 序列化为可哈希、稳定的 key（深度浅，按键名排序）。

    用于 append_filters 简单去重，不要求语义级别比较。
    """
    if not isinstance(params, dict) or not params:
        return ""
    try:
        items = sorted((str(k), str(v)) for k, v in params.items())
    except Exception:  # noqa: BLE001
        return repr(params)
    return "|".join(f"{k}={v}" for k, v in items)
