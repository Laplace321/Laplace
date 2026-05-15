"""
跨模型 fallback tool_call 格式兼容性测试。

模拟场景：Agent Round 1 用 Claude (obao) 成功调用了工具，
Round 2 需要 fallback 到 dashscope (qwen-plus)，
此时 messages 中包含 Claude 格式的 assistant + tool messages。

验证 _sanitize_tool_messages 能否正确清洗，让 dashscope 不报错。
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from server.llm import provider as _provider_mod  # noqa: E402

_sanitize_tool_messages = _provider_mod._sanitize_tool_messages


def test_sanitize_cases():
    """纯逻辑测试：验证各种脏格式的清洗效果。"""
    print("=" * 60)
    print("测试 1: _sanitize_tool_messages 清洗逻辑")
    print("=" * 60)

    # Case A: Claude message.model_dump() 真实输出
    # 来源: openai SDK ChatCompletionMessage.model_dump() 实际字段：
    #   content: null, refusal: null, role: "assistant", annotations: null,
    #   audio: null, function_call: null, tool_calls: [...]
    claude_messages = [
        {"role": "system", "content": "你是一个助手"},
        {"role": "user", "content": "d类特攻最高的光炮从者有哪些"},
        {
            "content": None,
            "refusal": None,
            "role": "assistant",
            "annotations": None,
            "audio": None,
            "function_call": None,
            "tool_calls": [
                {
                    "id": "toolu_01ABC",
                    "function": {
                        "arguments": '{"category": "np"}',
                        "name": "list_effects",
                    },
                    "type": "function",
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "toolu_01ABC",
            "content": '{"total": 57, "effects": ["damageNpSP"]}',
        },
    ]

    sanitized = _sanitize_tool_messages(claude_messages)

    print("\n--- Case A: Claude model_dump() 典型输出 ---")
    for i, msg in enumerate(sanitized):
        print(f"  [{i}] role={msg['role']}")
        if msg["role"] == "assistant" and "tool_calls" in msg:
            print(f"      content={repr(msg.get('content'))}")
            print(f"      tool_calls count={len(msg['tool_calls'])}")
            for tc in msg["tool_calls"]:
                print(f"        id={tc.get('id')}, type={tc.get('type')}")
                func = tc.get("function", {})
                print(f"        function.name={func.get('name')}")
                print(f"        function.arguments={repr(func.get('arguments'))}")
                # 验证 arguments 是合法 JSON 字符串
                args = func.get("arguments", "{}")
                try:
                    json.loads(args)
                    print("        ✅ arguments 是合法 JSON")
                except Exception as e:
                    print(f"        ❌ arguments 不是合法 JSON: {e}")
            # 验证没有多余字段
            extra_keys = set(msg.keys()) - {"role", "content", "tool_calls"}
            if extra_keys:
                print(f"      ❌ 多余字段: {extra_keys}")
            else:
                print("      ✅ 无多余字段")

    # Case B: arguments 是 dict 而非 JSON 字符串（某些 SDK 序列化差异）
    dict_args_messages = [
        {"role": "user", "content": "test"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {
                        "name": "search_servants",
                        "arguments": {"filters": {"npEffect": "damageNpSP"}, "npTarget": "all"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "content": '{"total": 10}',
        },
    ]

    sanitized_b = _sanitize_tool_messages(dict_args_messages)

    print("\n--- Case B: arguments 是 dict（非 JSON 字符串）---")
    assistant_msg = sanitized_b[1]
    func_args = assistant_msg["tool_calls"][0]["function"]["arguments"]
    print(f"  arguments type: {type(func_args).__name__}")
    print(f"  arguments value: {repr(func_args)}")
    if isinstance(func_args, str):
        try:
            json.loads(func_args)
            print("  ✅ dict 已正确转换为 JSON 字符串")
        except Exception:
            print("  ❌ 转换失败")
    else:
        print("  ❌ 仍然是 dict，未转换")

    # Case C: arguments 是非法 JSON 字符串
    bad_json_messages = [
        {"role": "user", "content": "test"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_456",
                    "type": "function",
                    "function": {
                        "name": "list_effects",
                        "arguments": "{category: np}",  # 非法 JSON（无引号）
                    },
                }
            ],
        },
    ]

    sanitized_c = _sanitize_tool_messages(bad_json_messages)

    print("\n--- Case C: arguments 是非法 JSON 字符串 ---")
    func_args_c = sanitized_c[1]["tool_calls"][0]["function"]["arguments"]
    print(f"  arguments value: {repr(func_args_c)}")
    if func_args_c == "{}":
        print("  ✅ 非法 JSON 已降级为空对象")
    else:
        print("  ❌ 未正确处理非法 JSON")

    # Case D: arguments 是 None
    none_args_messages = [
        {"role": "user", "content": "test"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_789",
                    "type": "function",
                    "function": {
                        "name": "list_effects",
                        "arguments": None,
                    },
                }
            ],
        },
    ]

    sanitized_d = _sanitize_tool_messages(none_args_messages)

    print("\n--- Case D: arguments 是 None ---")
    func_args_d = sanitized_d[1]["tool_calls"][0]["function"]["arguments"]
    print(f"  arguments value: {repr(func_args_d)}")
    if func_args_d == "{}":
        print("  ✅ None 已降级为空对象")
    else:
        print("  ❌ 未正确处理 None")


async def test_dashscope_with_dirty_messages():
    """实际调用测试：用 Claude 格式的脏 messages 发给 dashscope，验证清洗后能否正常工作。"""
    print("\n" + "=" * 60)
    print("测试 2: 实际发送清洗后的 messages 到 dashscope")
    print("=" * 60)

    # 检查 dashscope 是否配置
    dashscope_key = os.environ.get("DASHSCOPE_API_KEY")
    if not dashscope_key:
        print("  ⚠️ 未配置 DASHSCOPE_API_KEY，跳过实际调用测试")
        return

    from server.llm.adapters.dashscope_adapter import DashscopeAdapter

    adapter = DashscopeAdapter("dashscope", "", dashscope_key)

    # 模拟 Claude Round 1 之后的 messages（包含脏 tool_call）
    dirty_messages = [
        {
            "role": "system",
            "content": "你是一个 FGO 从者数据库助手。使用工具来回答用户的问题。",
        },
        {"role": "user", "content": "d类特攻最高的光炮从者有哪些"},
        # Claude 格式的 assistant message（Round 1 结果）
        {
            "role": "assistant",
            "content": None,
            "refusal": None,
            "audio": None,
            "tool_calls": [
                {
                    "id": "toolu_01XYZ",
                    "type": "function",
                    "index": 0,
                    "function": {
                        "name": "list_effects",
                        "arguments": '{"category": "np"}',
                    },
                }
            ],
        },
        # tool 执行结果
        {
            "role": "tool",
            "tool_call_id": "toolu_01XYZ",
            "content": json.dumps(
                {
                    "total": 3,
                    "effects": [
                        {"name": "damageNpSP", "zh": "宝具特攻"},
                        {"name": "damageNpIndividuality", "zh": "特性特攻"},
                    ],
                },
                ensure_ascii=False,
            ),
        },
    ]

    # 简化的 tools 定义（只需要一个就够验证）
    tools = [
        {
            "type": "function",
            "function": {
                "name": "list_effects",
                "description": "列出所有可用效果",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "效果类别",
                        }
                    },
                },
            },
        }
    ]

    # --- 测试 A: 不清洗，直接发脏 messages ---
    print("\n--- 2A: 不清洗，直接发脏 messages 给 dashscope ---")
    try:
        result = await adapter.agent_completion(
            model="qwen-plus",
            messages=dirty_messages,
            tools=tools,
            max_tokens=256,
            temperature=0.1,
        )
        print(f"  ✅ 竟然成功了！output: {result.get('output_text', '')[:100]}")
    except Exception as e:
        error_str = str(e)
        print(f"  ❌ 报错（预期中）: {error_str[:200]}")
        if "arguments" in error_str.lower() or "json" in error_str.lower() or "parameter" in error_str.lower():
            print("  → 确认是 tool_call 格式问题 ✅")
        else:
            print("  → 不是格式问题，可能是其他原因")

    # --- 测试 B: 清洗后再发 ---
    print("\n--- 2B: 清洗后发 messages 给 dashscope ---")
    clean_messages = _sanitize_tool_messages(dirty_messages)

    print("  清洗后的 assistant message:")
    assistant = clean_messages[2]
    print(f"    content={repr(assistant.get('content'))}")
    print(f"    keys={list(assistant.keys())}")
    if "tool_calls" in assistant:
        tc = assistant["tool_calls"][0]
        print(f"    tc keys={list(tc.keys())}")
        print(f"    tc.function.arguments={repr(tc['function']['arguments'])}")

    try:
        result = await adapter.agent_completion(
            model="qwen-plus",
            messages=clean_messages,
            tools=tools,
            max_tokens=256,
            temperature=0.1,
        )
        output = result.get("output_text", "") or "(tool_call, no text)"
        print(f"  ✅ 清洗后成功! output: {output[:200]}")
        if result.get("has_tool_call"):
            print(f"  tool_calls: {result.get('tool_calls')}")
    except Exception as e:
        print(f"  ❌ 清洗后仍然失败: {e}")


if __name__ == "__main__":
    test_sanitize_cases()
    asyncio.run(test_dashscope_with_dirty_messages())
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
