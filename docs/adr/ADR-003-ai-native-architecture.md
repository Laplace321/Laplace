# ADR-003: AI Native 架构 — LLM 意图解析 + Query Executor

- **日期**: 2026-05-05
- **状态**: 已采纳

## 背景
从静态 Demo 升级为 AI Native 对话式产品。

## 决策
LLM 只做意图解析和结果格式化，数据查询由中间层 Query Executor 执行。

## 理由
分离关注点，LLM 输出的结构化 JSON 指令可预测、可测试，避免 LLM 直接操作数据带来的不可靠性。
