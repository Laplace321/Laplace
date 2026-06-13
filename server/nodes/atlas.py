"""Pipeline B 节点 — Atlas 知识问答（活动/卡池/主线/素材）。

迁移自 server/pipeline.py 的 ``_handle_atlas_pipeline`` 与 ``_extract_atlas_query`` /
``_verify_atlas_facts``。本节点行为与原函数完全等价，仅把入参/出参改为 PipelineState。
"""

from __future__ import annotations

import json as _json
import re
import time
from collections.abc import AsyncGenerator

from server.atlas_index import AtlasQueryParams, get_atlas_index
from server.graph.state import PipelineState
from server.llm import StreamMetadata, chat_completion, chat_completion_stream, extract_json_object
from server.logger import log_trace_event


async def _extract_atlas_query(user_message: str, trace_id: str) -> dict | None:
    """独立 LLM 调用：从用户查询中提取 Atlas 结构化参数。

    Stage 0 只做链路分类不提取参数，链路 B 需要单独调用此函数提取
    name/entry_type/year_month 等结构化字段。

    Returns:
        提取到的参数字典，失败时返回 None（由调用方降级处理）。
    """
    extraction_prompt = """你是 FGO 知识查询参数提取器。从用户问题中提取结构化查询参数。

## 输出字段
- `name`: 具体的活动名/卡池名/关卡名/素材名/从者名（字符串），不要填入整个问句
- `entry_type`: 条目类型（event=活动/war=主线关卡/gacha=卡池/item=素材）
- `tag`: 标签（如 event_type:campaign，可选）
- `year_month`: 时间（YYYY-MM 格式，可选）

## 输出格式
严格按 JSON 格式输出，只包含能从问题中提取到的字段，无法确定的字段不要填：
```json
{"name": "梅林", "entry_type": "gacha"}
```

## 示例
用户："梅林什么时候复刻" → {"name": "梅林", "entry_type": "gacha"}
用户："最近有什么活动" → {"entry_type": "event"}
用户："特异点F是什么" → {"name": "特异点F", "entry_type": "war"}
用户："龙之牙在哪里掉" → {"name": "龙之牙", "entry_type": "item"}
用户："去年周年庆" → {"entry_type": "event", "year_month": "2025-07"}
"""
    try:
        result = await chat_completion(
            system_prompt=extraction_prompt,
            user_message=user_message,
            temperature=0.0,
            json_mode=True,
        )
        text = result.get("text", "")
        extracted = _json.loads(extract_json_object(text))
        await log_trace_event(
            trace_id,
            "atlas_query_extraction",
            {"query": user_message, "extracted": extracted},
        )
        return extracted
    except Exception as extraction_err:  # noqa: BLE001
        print(f"⚠️ [{trace_id}] Atlas 参数提取失败，降级原始查询: {extraction_err}")
        return None


def _verify_atlas_facts(reply: str, atlas) -> bool:
    """轻量级事实验证：检查回复中提到的实体是否存在于 Atlas 索引中。"""
    quoted_entities = re.findall(r"[\u201c\u201d](.*?)[\u201c\u201d]", reply)
    if not quoted_entities:
        return True

    verified_count = 0
    total_count = 0
    for entity in quoted_entities:
        if len(entity) < 2:
            continue
        total_count += 1
        if atlas.verify_fact("name", entity):
            verified_count += 1

    if total_count == 0:
        return True
    return verified_count / total_count >= 0.7


async def atlas_node(state: PipelineState) -> PipelineState:
    """Pipeline B 主节点：Atlas 检索 → LLM 生成回复 → 事实校验。

    输入字段：``user_message`` / ``trace_id`` / ``request_start`` / ``trace_total_tokens`` /
    ``model_used`` / ``atlas_query``（可选）
    输出字段：``reply`` / ``servants=[]`` / ``count=0`` / ``query={"mode": "atlas_pipeline"}`` /
    ``model_used`` / ``trace_total_tokens``
    """
    atlas = get_atlas_index()

    # 优先使用路由 LLM 提取的结构化参数
    query_source = "structured"
    atlas_query = state.atlas_query
    if atlas_query:
        params = AtlasQueryParams(**atlas_query)
    else:
        # Stage 0 只做分类不提取参数 → 独立 LLM 调用提取 atlas_query
        query_source = "llm_extraction"
        extracted_query = await _extract_atlas_query(state.user_message, state.trace_id)
        if extracted_query:
            params = AtlasQueryParams(**extracted_query)
            atlas_query = extracted_query
        else:
            query_source = "raw_fallback"
            params = AtlasQueryParams(name=state.user_message)

    results = atlas.search(params)

    await log_trace_event(
        state.trace_id,
        "atlas_search",
        {
            "query": state.user_message,
            "result_count": len(results),
            "query_source": query_source,
            "atlas_query": atlas_query,
        },
    )

    # 结构化搜索无结果时，降级到原始消息匹配
    if not results and query_source == "structured":
        query_source = "raw_fallback"
        params = AtlasQueryParams(name=state.user_message)
        results = atlas.search(params)
        await log_trace_event(
            state.trace_id,
            "atlas_search_fallback",
            {"query": state.user_message, "result_count": len(results), "query_source": query_source},
        )

    if not results:
        state.reply = "抱歉，我在活动/卡池/主线数据库中未找到相关信息。你可以尝试更具体的关键词，如活动名称或时间段。"
        state.servants = []
        state.count = 0
        state.query = {"mode": "atlas_pipeline"}
        await log_trace_event(
            state.trace_id,
            "final",
            {
                "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
                "result": "atlas_no_match",
                "mode": "atlas_pipeline",
                "total_tokens": state.trace_total_tokens,
            },
        )
        return state

    # 构建上下文摘要供 LLM 参考
    context_lines = []
    for item in results[:10]:
        summary_parts = [f"{k}: {v}" for k, v in item.items() if k != "entry_key"]
        context_lines.append("- " + ", ".join(summary_parts))
    atlas_context = "\n".join(context_lines)

    generation_prompt = (
        "你是 FGO 知识助手。根据以下 Atlas 数据库检索结果回答用户问题。\n"
        "要求：\n"
        "1. 仅基于提供的数据回答，不要编造信息\n"
        "2. 使用中文自然语言，面向玩家\n"
        "3. 如果数据不足以完整回答，明确告知哪些信息缺失\n\n"
        f"检索结果：\n{atlas_context}\n\n"
        f"用户问题：{state.user_message}"
    )

    gen_result = await chat_completion(
        system_prompt="You are a helpful AI assistant.",
        user_message=generation_prompt,
        temperature=0.3,
        json_mode=False,
    )
    reply = gen_result.get("text", "").strip()
    gen_usage = gen_result.get("_usage", {})
    state.trace_total_tokens += gen_usage.get("total_tokens", 0)
    state.model_used = gen_result.get("_model", state.model_used)

    # 轻量级事实校验
    verified = _verify_atlas_facts(reply, atlas)
    await log_trace_event(state.trace_id, "fact_verify", {"verified": verified})

    await log_trace_event(
        state.trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
            "result": "atlas_pipeline",
            "mode": "atlas_pipeline",
            "atlas_results": len(results),
            "total_tokens": state.trace_total_tokens,
        },
    )

    state.reply = reply
    state.servants = []
    state.count = 0
    state.query = {"mode": "atlas_pipeline"}
    return state


async def atlas_stream_node(state: PipelineState) -> AsyncGenerator[dict | PipelineState, None]:
    """Pipeline B（SSE 流式版） — 与 ``atlas_node`` 行为等价，差异仅在 LLM 流式 yield delta。

    yield 顺序：
    1. ``thinking phase=routing`` "识别为 Atlas 知识检索，正在检索..."
    2. （无命中时）``delta`` 一次性 + 终态 state
    3. 命中时：``delta`` * N（chat_completion_stream 逐 chunk）+ 终态 state
    """
    yield {
        "type": "thinking",
        "data": {"phase": "routing", "message": "识别为Atlas 知识检索，正在检索..."},
    }

    atlas = get_atlas_index()
    query_source = "structured"
    atlas_query = state.atlas_query
    if atlas_query:
        params = AtlasQueryParams(**atlas_query)
    else:
        query_source = "llm_extraction"
        extracted_query = await _extract_atlas_query(state.user_message, state.trace_id)
        if extracted_query:
            params = AtlasQueryParams(**extracted_query)
            atlas_query = extracted_query
        else:
            query_source = "raw_fallback"
            params = AtlasQueryParams(name=state.user_message)

    results = atlas.search(params)
    await log_trace_event(
        state.trace_id,
        "atlas_search",
        {
            "query": state.user_message,
            "result_count": len(results),
            "query_source": query_source,
            "atlas_query": atlas_query,
        },
    )

    if not results and query_source == "structured":
        query_source = "raw_fallback"
        params = AtlasQueryParams(name=state.user_message)
        results = atlas.search(params)
        await log_trace_event(
            state.trace_id,
            "atlas_search_fallback",
            {"query": state.user_message, "result_count": len(results), "query_source": query_source},
        )

    if not results:
        no_match_reply = (
            "抱歉，我在活动/卡池/主线数据库中未找到相关信息。你可以尝试更具体的关键词，如活动名称或时间段。"
        )
        state.reply = no_match_reply
        state.servants = []
        state.count = 0
        state.query = {"mode": "atlas_pipeline"}
        yield {"type": "delta", "data": {"text": no_match_reply}}
        await log_trace_event(
            state.trace_id,
            "final",
            {
                "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
                "result": "atlas_no_match",
                "mode": "atlas_pipeline",
                "total_tokens": state.trace_total_tokens,
            },
        )
        yield state
        return

    context_lines = []
    for item in results[:10]:
        summary_parts = [f"{k}: {v}" for k, v in item.items() if k != "entry_key"]
        context_lines.append("- " + ", ".join(summary_parts))
    atlas_context = "\n".join(context_lines)

    generation_prompt = (
        "你是 FGO 知识助手。根据以下 Atlas 数据库检索结果回答用户问题。\n"
        "要求：\n"
        "1. 仅基于提供的数据回答，不要编造信息\n"
        "2. 使用中文自然语言，面向玩家\n"
        "3. 如果数据不足以完整回答，明确告知哪些信息缺失\n\n"
        f"检索结果：\n{atlas_context}\n\n"
        f"用户问题：{state.user_message}"
    )

    full_reply_parts: list[str] = []
    metadata = StreamMetadata()
    try:
        async for chunk in chat_completion_stream(
            system_prompt="You are a helpful AI assistant.",
            user_message=generation_prompt,
            temperature=0.3,
            metadata=metadata,
        ):
            full_reply_parts.append(chunk)
            yield {"type": "delta", "data": {"text": chunk}}
        reply = "".join(full_reply_parts).strip()
        if not reply:
            raise ValueError("Empty response from LLM")
    except Exception as gen_err:  # noqa: BLE001
        if full_reply_parts:
            reply = "".join(full_reply_parts).strip()
        else:
            reply = "抱歉，知识检索回复生成失败，请稍后重试。"
            try:
                yield {"type": "delta", "data": {"text": reply}}
            except Exception:  # noqa: BLE001
                pass
        await log_trace_event(
            state.trace_id,
            "atlas_generation_error",
            {"error": str(gen_err)},
        )

    state.trace_total_tokens += metadata.usage.get("total_tokens", 0)
    if metadata.model:
        state.model_used = metadata.model

    verified = _verify_atlas_facts(reply, atlas)
    await log_trace_event(state.trace_id, "fact_verify", {"verified": verified})

    await log_trace_event(
        state.trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
            "result": "atlas_pipeline",
            "mode": "atlas_pipeline",
            "atlas_results": len(results),
            "total_tokens": state.trace_total_tokens,
        },
    )

    state.reply = reply
    state.servants = []
    state.count = 0
    state.query = {"mode": "atlas_pipeline"}
    yield state
