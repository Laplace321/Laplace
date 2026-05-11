"""
Laplace — Agent Loop

Agentic Tool Use 核心引擎。
负责多轮 tool 调用的编排：LLM → tool_calls → handler → LLM → ... → message。
使用 Chat Completions API（/v1/chat/completions）统一协议。
"""

from __future__ import annotations

import inspect
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.agent.agent_prompt import AGENT_SYSTEM_PROMPT
from server.agent.tool_defs import build_agent_tools
from server.llm_client import agent_completion

# 需要保留完整从者数据的 tool 名称（用于卡片渲染）
_CARD_TOOLS = {"search_servants", "lookup_servant", "compare_servants"}


@dataclass
class AgentResult:
    """Agent 路由最终结果。"""

    reply: str  # LLM 最终回复文本
    tool_trace: list[dict] = field(default_factory=list)  # 每轮 tool 调用记录
    rounds: int = 0  # 总轮次
    total_tokens: int = 0  # 累计 token 用量
    elapsed_ms: float = 0.0  # 总耗时
    is_fallback: bool = False  # 是否降级
    servants_data: list[dict] = field(default_factory=list)  # 从者卡片数据


async def agent_route(
    user_message: str,
    tool_handlers: dict[str, Callable],
    trace_id: str,
    max_rounds: int = 5,
) -> AgentResult:
    """Agent 多轮路由主循环（Chat Completions API）。

    流程：
    1. 首轮：messages=[system, user] + tools 发给 LLM
    2. 检查 assistant message：
       - 含 tool_calls → 执行 handler → 追加 assistant + tool messages → 下一轮
       - 无 tool_calls → 提取文本 → 结束
    3. 超过 max_rounds → 强制终止，返回降级回复
    """
    tools = build_agent_tools()
    start_time = time.monotonic()
    tool_trace: list[dict] = []
    total_tokens = 0
    servants_data: list[dict] = []

    # Chat Completions messages 列表（累积式）
    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    for round_num in range(1, max_rounds + 1):
        print(f"🔄 [{trace_id}] Agent Round {round_num}, messages: {len(messages)}")

        # 调用 LLM
        try:
            response = await agent_completion(
                messages=messages,
                tools=tools,
                temperature=0.1,
            )
        except Exception as e:
            print(f"❌ [{trace_id}] Agent Round {round_num} LLM 调用失败: {e}")
            return AgentResult(
                reply="抱歉，我遇到了一些问题，请稍后再试。",
                tool_trace=tool_trace,
                rounds=round_num,
                total_tokens=total_tokens,
                elapsed_ms=(time.monotonic() - start_time) * 1000,
                is_fallback=True,
                servants_data=servants_data,
            )

        usage = response.get("usage", {})
        total_tokens += usage.get("total_tokens", 0)

        # 非 tool call → Agent 直接回复，结束循环
        if not response.get("has_tool_call"):
            reply_text = response.get("output_text") or ""
            print(f"✅ [{trace_id}] Agent 完成，共 {round_num} 轮，{total_tokens} tokens")
            return AgentResult(
                reply=reply_text or "抱歉，我无法生成回复。",
                tool_trace=tool_trace,
                rounds=round_num,
                total_tokens=total_tokens,
                elapsed_ms=(time.monotonic() - start_time) * 1000,
                servants_data=servants_data,
            )

        # 有 tool call → 先追加 assistant 原始 message（含 tool_calls）
        raw_message = response.get("raw_message", {})
        messages.append(raw_message)

        # 逐个执行 handler，追加 tool role message
        tool_calls = response.get("tool_calls", [])
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            call_id = tc.get("call_id", "")
            raw_args = tc.get("arguments", "{}")

            # 解析参数
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}

            print(f"  🔧 [{trace_id}] R{round_num}: {tool_name}({json.dumps(args, ensure_ascii=False)[:200]})")

            # 执行 handler
            handler = tool_handlers.get(tool_name)
            if handler is None:
                tool_result: dict[str, Any] = {"error": f"未知工具: {tool_name}"}
            else:
                try:
                    result = handler(args)
                    if inspect.isawaitable(result):
                        result = await result
                    tool_result = result
                except Exception as e:
                    print(f"  ❌ [{trace_id}] R{round_num}: {tool_name} 执行失败: {e}")
                    tool_result = {"error": f"工具执行失败: {e}"}

            # 提取完整从者数据（供卡片渲染，不传给 LLM）
            if tool_name in _CARD_TOOLS:
                full = tool_result.pop("_full_servants", None)
                if full:
                    servants_data = full  # 保留最后一次的从者数据

            # 记录 trace
            tool_trace.append(
                {
                    "round": round_num,
                    "tool": tool_name,
                    "args": args,
                    "result_summary": _summarize_result(tool_result),
                }
            )

            # 追加 tool role message（Chat Completions 格式）
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
            )

    # 超过 max_rounds
    print(f"⚠️ [{trace_id}] Agent 超过最大轮次 {max_rounds}，强制终止")
    return AgentResult(
        reply="抱歉，查询过程比较复杂，我暂时无法完成处理。请尝试简化你的问题。",
        tool_trace=tool_trace,
        rounds=max_rounds,
        total_tokens=total_tokens,
        elapsed_ms=(time.monotonic() - start_time) * 1000,
        is_fallback=True,
        servants_data=servants_data,
    )


def _summarize_result(result: dict) -> str:
    """生成 tool 结果的简短摘要（用于 trace 日志，避免过大）。"""
    if "error" in result:
        return f"error: {result['error']}"
    total = result.get("total")
    if total is not None:
        return f"total={total}"
    return json.dumps(result, ensure_ascii=False)[:100]
