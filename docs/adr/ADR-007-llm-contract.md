# ADR-007: LLM Contract — JSON Schema + Pydantic 校验

- **日期**: 2026-05-05
- **状态**: 已采纳

## 背景
`llm_client.py` 原先依赖手动截取 Markdown code fence 后执行 `json.loads()`，在模型漏写 JSON 或输出额外文本时不稳定。

## 决策
首阶段意图解析优先使用 OpenAI-compatible `response_format/json_schema`；若模型网关明确不支持，则自动降级到普通文本 JSON 提取。所有 JSON 输出必须通过 `server/schemas.py` 中的 Pydantic Contract 校验后才能进入 Query Executor。

## 理由
在 API 层和应用层双重约束 LLM 输出，减少解析幻觉和格式漂移，同时保持与现有模型回退链兼容。
