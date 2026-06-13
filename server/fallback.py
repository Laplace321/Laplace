"""Fallback / Agent 兜底模块 — 模板回复、分类解析、SSE 辅助。

从 main.py 抽取的 Agent 兜底相关逻辑，包括：
- _build_oneshot_context — OneShot 空结果上下文构建
- FALLBACK_TEMPLATES — greeting/out_of_scope/unsupported 模板
- classify_agent_reply — Agent 回复分类标记解析
- sse_event — SSE 事件格式化辅助函数
"""

from __future__ import annotations

import json
import re

from server.translation import describe_filters

# === OneShot 空结果上下文 ===


def build_oneshot_context(skill_calls: list[dict]) -> str:
    """将 OneShot 已识别的筛选条件转为中文描述字符串，供 Agent fallback 使用。

    当 OneShot 执行结果为 0 时，将此上下文传给 Agent，
    让 Agent 知道之前尝试过什么条件，给出更有针对性的回答。
    """
    filters = describe_filters(skill_calls)
    if filters:
        return "、".join(filters)
    return "（未识别到具体筛选条件）"


# === Agent 兜底辅助 ===

FALLBACK_TEMPLATES: dict[str, str] = {
    "GREETING": (
        "你好！我是 **Laplace**，一个 FGO 智能数据助手。"
        "你可以用日常语言向我提问，我会从数据库中检索和分析从者信息。\n\n"
        "**我能帮你做这些事：**\n"
        "- **条件筛选** — 按职阶、星级、配卡、属性、特性等条件筛选从者\n"
        "- **效果搜索** — 搜索拥有特定效果的从者（如充能、增伤、无敌、闪避等）\n"
        "- **从者详情** — 查看某个从者的完整数据和技能信息\n"
        "- **从者对比** — 把几个从者放在一起比较，分析各自优劣\n\n"
        "**试试这样问我：**\n"
        '- "30自充以上的五星Caster"\n'
        '- "有增伤技能的从者"\n'
        '- "对比梅林和斯卡蒂"\n'
        '- "查一下村正"\n'
        '- "剑阶戴冠配队推荐"\n'
        '- "戴冠战机制是什么"'
    ),
    "OUT_OF_SCOPE": (
        "抱歉，这个问题超出了我的能力范围。"
        "我是一个 FGO 从者数据助手，只能帮你查询和分析从者信息。\n\n"
        "你可以试试问我：\n"
        '- "有哪些5星弓阶从者"\n'
        '- "对比梅林和斯卡蒂"\n'
        '- "有无敌技能的从者"'
    ),
    "UNSUPPORTED": (
        "这个功能暂时还不支持。目前我只能帮你查询从者数据（筛选、搜索、对比），"
        "还不能做队伍搭配推荐、关卡攻略、礼装推荐等。\n\n"
        "你可以试试问我从者相关的查询，比如：\n"
        '- "50%以上充能的五星Caster"\n'
        '- "有增伤效果的从者"'
    ),
}


def _sanitize_agent_reply(reply: str) -> str:
    """清洗 Agent 回复中可能泄露给用户的技术标记。

    移除 <tool_call>...</tool_call> 块及残留的 JSON 工具调用片段，
    防止 Agent 生成的"思考过程"原样暴露给前端（违反 §9 零技术术语）。
    """
    # 移除 Agent 可能产生的各种 XML 标签块（tool_call/tool_use/search_tool_type 等）
    cleaned = re.sub(
        r"<[a-z_]+>\s*(?:\{.*?\})?\s*</[a-z_]+>",
        "",
        reply,
        flags=re.DOTALL,
    )
    # 移除残留的未闭合 XML 标签
    cleaned = re.sub(r"</?[a-z_]+>", "", cleaned)
    # 移除独立出现的 JSON 工具调用行（如 {"type": "search_servants", ...}）
    cleaned = re.sub(
        r'^\s*\{"type"\s*:\s*"[^"]+?".*?\}\s*$',
        "",
        cleaned,
        flags=re.MULTILINE,
    )
    # 合并连续空行为单个空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def classify_agent_reply(reply: str) -> tuple[str | None, str]:
    """解析 Agent 回复中的分类标记，返回 (category, clean_reply)。

    Agent Prompt 要求在无需调用工具时，以 [GREETING]/[OUT_OF_SCOPE]/[UNSUPPORTED] 开头。
    检测到标记后替换为标准化模板回复。
    非模板回复也会经过清洗，移除可能泄露的技术标记。
    """
    for tag in ("GREETING", "OUT_OF_SCOPE", "UNSUPPORTED"):
        if reply.strip().startswith(f"[{tag}]"):
            return tag, FALLBACK_TEMPLATES[tag]
    return None, _sanitize_agent_reply(reply)


def sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 事件。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
