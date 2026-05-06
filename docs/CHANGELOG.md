# Changelog — 项目里程碑

> 记录项目关键节点和版本演进历史。按时间倒序排列。

| 日期 | 事件 | 备注 |
| :--- | :--- | :--- |
| 2026-05-06 | Phase 5 完成 | P1（Filter Registry、知识配置分离、Chaldea 依赖边界、配置热更新、Trace Debug）+ P2（LLM Retry、前端 UX、异步日志、工程自动化 ruff + GitHub Actions CI） |
| 2026-05-06 | Thinking Steps SSE | 新增 SSE 流式端点，分阶段展示 AI 思考过程（解析→检索→生成），卡片先行渲染 |
| 2026-05-06 | Phase 5 Batch 2 - P0 | 完成数据入口单一化：extractor/np_charge_filter.py 从 191 行降至 52 行，复用 data_loader.py |
| 2026-05-06 | LLM API 迁移 | 从 Chat Completions API 迁移至 OpenAI Responses API（2025 推荐） |
| 2026-05-05 | Phase 5 Batch 1 | 完成 LLM Contract、Query Executor 回归测试、Schema Mirror 回归测试与真实 LLM JSON Schema smoke test |
| 2026-05-05 | Phase 5 启动 | 实现了全链路日志追踪（Logging）与数据预消化（Pre-digestion），补齐了宝具特效解析 |
| 2026-05-05 | Phase 4 完成 | 实现了 Two-Step RAG 架构（生成式 UI），分离了 LLM 总结文案与 UI 数据流 |
| 2026-05-05 | Phase 3 完成 | 实现了多语言映射、特性（Trait）匹配算法、宝具与配卡等从者深层属性过滤 |
| 2026-05-05 | Phase 2 完成 | 实现了 sync_chaldea.py 提取 5 个 Dart 文件的效果知识并与 LLM 集成 |
| 2026-05-05 | 架构升级 | 确立 Schema Mirror 策略，目标对标 Chaldea 全数据查询 |
| 2026-05-05 | AI Native v1 | 对话式查询上线（FastAPI + LLM 意图解析 + 从者卡片） |
| 2026-05-05 | Demo v1 完成 | 30% NP 自充筛选器 (Python + Web) |
| 2026-05-05 | 项目初始化 | 创建 OpenClaw 风格的项目骨架 |
