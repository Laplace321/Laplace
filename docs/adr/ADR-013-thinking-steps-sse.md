# ADR-013: Thinking Steps SSE 流式交互

- **日期**: 2026-05-06
- **状态**: 已采纳

## 背景
用户发送查询后只看到三个跳动圆点（typing indicator），3-8 秒的等待全程黑盒，体验差。

## 决策
1. 新增 `GET /api/chat/stream` SSE 端点，分阶段推送事件（thinking → servants → delta → done）
2. 前端使用 `fetch` + `ReadableStream` 消费 SSE，逐阶段渲染 Thinking Steps
3. 从者卡片在数据查询完成后立即展示（卡片先行），不等 RAG 生成
4. 保留原有 `POST /api/chat` 端点向后兼容
5. 提取 `_build_context()` 共享函数，供两个端点复用

## 理由
1. 零额外 Token 消耗 — 只改变传输方式，不改变 LLM 调用逻辑
2. 参考主流 AI 产品（Perplexity / ChatGPT）的 Thinking Steps 交互模式
3. 用户感知等待时间从"3-8s 黑盒"降低为"每阶段 1-3s 有持续反馈"
