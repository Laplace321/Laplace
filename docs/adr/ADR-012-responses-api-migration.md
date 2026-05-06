# ADR-012: LLM 客户端迁移至 Responses API

- **日期**: 2026-05-06
- **状态**: 已采纳

## 背景
OpenAI 官方推荐 Responses API 替代 Chat Completions API（2025 年起）。

## 决策
1. 端点: `/v1/chat/completions` → `/v1/responses`
2. 参数: `messages` → `input`, system role → `instructions`
3. 结构化输出: `response_format` → `text.format`
4. 响应解析: `choices[0].message.content` → `output_text`
5. 保留 fallback 链和 Pydantic 校验逻辑不变

## 理由
1. Responses API 是 OpenAI 最新推荐接口，支持更强大的 agentic 功能
2. 更好的缓存利用率（40-80% 提升）
3. 更清晰的语义（instructions vs input 分离）
4. 未来 GPT-5+ 模型将优先支持 Responses API
