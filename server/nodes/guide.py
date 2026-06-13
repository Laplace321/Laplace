"""Pipeline C 节点 — 攻略问答（BM25 检索 + 来源标注）。

迁移自 server/pipeline.py 的 ``_handle_guide_pipeline`` 与一系列辅助函数。
本节点为非流式版本，行为与原 ``_handle_guide_pipeline`` 完全等价。
SSE 流式版本由 stream_event_generator 中的 inline 实现保留（Task 5 才统一）。
"""

from __future__ import annotations

import time

from server.graph.state import PipelineState
from server.guide_retriever import GuideRetriever
from server.llm import chat_completion
from server.logger import log_trace_event

# 职阶关键词 → 对应 tag（优先匹配中文简称/俗称）
_CLASS_TAG_MAP: dict[str, str] = {
    "剑": "saber",
    "剑阶": "saber",
    "剑冠": "saber",
    "弓": "archer",
    "弓阶": "archer",
    "弓冠": "archer",
    "枪": "lancer",
    "枪阶": "lancer",
    "枪冠": "lancer",
    "骑": "rider",
    "骑阶": "rider",
    "骑冠": "rider",
    "术": "caster",
    "术阶": "caster",
    "术冠": "caster",
    "杀": "assassin",
    "杀阶": "assassin",
    "杀冠": "assassin",
    "狂": "berserker",
    "狂阶": "berserker",
    "狂冠": "berserker",
    "ex": "extra",
    "EX": "extra",
    "ex冠": "extra",
    "EX冠": "extra",
}


def _extract_guide_tags(query: str) -> list[str]:
    """从用户查询中提取攻略相关的 tag 关键词，用于缩小 BM25 检索范围。"""
    tags: list[str] = []
    for keyword, tag in _CLASS_TAG_MAP.items():
        if keyword in query and tag not in tags:
            tags.append(tag)
    return tags


def _prepare_guide_context(user_message: str) -> tuple[list, set[str], dict[str, str]] | None:
    """BM25 检索攻略文档并构建上下文。

    Returns:
        (chunks, source_labels, source_authors) 或 None（无结果时）
    """
    retriever = GuideRetriever()
    guide_tags = _extract_guide_tags(user_message)
    chunks = retriever.search(user_message, tags=guide_tags or None, top_k=3)

    if not chunks:
        return None

    source_labels: set[str] = set()
    source_authors: dict[str, str] = {}
    for chunk in chunks:
        title = chunk.metadata.get("title", "攻略")
        if title and title != "攻略":
            source_labels.add(title)
            author = chunk.metadata.get("author", "")
            if author:
                source_authors[title] = author

    return chunks, source_labels, source_authors


def _build_guide_context_text(chunks: list) -> str:
    """将 BM25 检索结果拼接为 LLM 上下文字符串。"""
    context_lines = []
    for chunk in chunks:
        title = chunk.metadata.get("title", "攻略")
        section = chunk.metadata.get("section", "")
        header = f"【{title}】" + (f" {section}" if section else "")
        context_lines.append(f"{header}\n{chunk.content}")
    return "\n\n---\n\n".join(context_lines)


def _build_guide_generation_prompt(guide_context: str, user_message: str) -> str:
    """构建链路 C generation 阶段的 system prompt。"""
    return (
        "你是 FGO 攻略助手。根据以下攻略文档内容回答用户问题。\n\n"
        "## 知识范围\n"
        "- 仅基于提供的攻略内容回答，严禁使用自身训练知识补充或猜测\n"
        "- 如果攻略中未涉及用户问的内容，必须明确说「攻略中暂未收录这部分内容」\n"
        "- 如果攻略内容不足以完整回答，明确告知哪些部分缺少资料\n\n"
        "## 格式要求（必须严格遵守）\n"
        "1. **开头直答**：回复第一段用 1-2 句话直接回答用户的核心问题，让用户立刻获得答案\n"
        "2. **分段标题**：用 `###` 三级标题划分不同话题段落（如阵容、打法、注意事项等），"
        "每个标题下内容控制在 3-5 句以内\n"
        "3. **加粗重点**：用 **加粗** 标记关键信息（从者名称、核心机制、必要条件、数值门槛）\n"
        "4. **列表并列**：并列选项（多套阵容、多个从者推荐、多步操作）使用 `- ` 无序列表\n"
        "5. **精简表达**：每段话不超过 3 句，删除「接下来我们看」等过渡废话，"
        "优先用短句和列表替代长段落\n"
        "6. **禁止冗余**：不要复述用户的问题，不要写开头寒暄，不要添加总结段\n"
        "7. **总长度控制**：整体回复控制在 600 字以内，信息密度优先于面面俱到\n\n"
        "## 禁止事项\n"
        "- 不要出现文件名、文件路径、内部标题格式（如 [xxx] > yyy）\n"
        "- 不要出现技术术语或系统实现细节\n"
        "- 不要自行添加来源标注（系统会自动处理）\n"
        "- 不要使用一级标题 `#` 或二级标题 `##`\n"
        "- **禁止使用 Markdown 表格**（`|---|` 语法），改用列表呈现对比信息\n\n"
        f"攻略内容：\n{guide_context}\n\n"
        f"用户问题：{user_message}"
    )


def _format_source_suffix(source_labels: set[str], source_authors: dict[str, str]) -> str:
    """格式化来源标注后缀。"""
    if not source_labels:
        return ""
    labels_with_author = []
    for title in sorted(source_labels):
        author = source_authors.get(title)
        if author:
            labels_with_author.append(f"{title}（作者：**{author}**）")
        else:
            labels_with_author.append(title)
    return "\n\n📖 参考：" + ", ".join(labels_with_author)


async def guide_node(state: PipelineState) -> PipelineState:
    """Pipeline C 主节点：BM25 检索攻略文档 → LLM 生成回复 → 来源标注。"""
    result = _prepare_guide_context(state.user_message)

    await log_trace_event(
        state.trace_id,
        "guide_search",
        {"query": state.user_message, "result_count": len(result[0]) if result else 0},
    )

    if result is None:
        state.reply = "抱歉，我在攻略库中未找到相关内容。你可以尝试更具体的关键词，如职阶名或关卡名。"
        state.servants = []
        state.count = 0
        state.query = {"mode": "guide_pipeline"}
        await log_trace_event(
            state.trace_id,
            "final",
            {
                "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
                "result": "guide_no_match",
                "mode": "guide_pipeline",
                "total_tokens": state.trace_total_tokens,
            },
        )
        return state

    chunks, source_labels, source_authors = result
    guide_context = _build_guide_context_text(chunks)
    generation_prompt = _build_guide_generation_prompt(guide_context, state.user_message)

    gen_result = await chat_completion(
        system_prompt=generation_prompt,
        user_message=state.user_message,
        temperature=0.3,
        json_mode=False,
    )
    reply = gen_result.get("text", "").strip()
    gen_usage = gen_result.get("_usage", {})
    state.trace_total_tokens += gen_usage.get("total_tokens", 0)
    state.model_used = gen_result.get("_model", state.model_used)

    reply += _format_source_suffix(source_labels, source_authors)

    await log_trace_event(
        state.trace_id,
        "final",
        {
            "total_time_ms": round((time.monotonic() - state.request_start) * 1000, 2),
            "result": "guide_pipeline",
            "mode": "guide_pipeline",
            "guide_chunks": len(chunks),
            "sources": list(source_labels),
            "total_tokens": state.trace_total_tokens,
        },
    )

    state.reply = reply
    state.servants = []
    state.count = 0
    state.query = {"mode": "guide_pipeline"}
    return state


# 公开辅助函数：SSE 流式实现（pipeline.stream_event_generator）仍直接 import 使用，
# Task 5 统一后再清理。
__all__ = [
    "guide_node",
    "_prepare_guide_context",
    "_build_guide_context_text",
    "_build_guide_generation_prompt",
    "_format_source_suffix",
    "_extract_guide_tags",
]
