# Memory — 项目记忆（热层索引）

> **分层记忆架构**：本文件为热层，每次会话必读（~50 行）。
> - 🔴 **热层** `MEMORY.md` — 当前迭代计划 + 活跃问题 + 技术备忘 + ADR 索引
> - 🟡 **温层** `docs/adr/ADR-*.md` — 架构决策详情，涉及相关架构时按需读取
> - 🟢 **冷层** `docs/CHANGELOG.md` — 里程碑历史归档，回顾历史时才读

## 当前迭代计划 (Next Steps)

1. **Phase 5 P1 全部完成，P2 推进中**：P1（5/5 完成）+ P2 LLM 调用可靠性补强已完成。剩余 P2（前端基础体验补齐、异步日志非阻塞）。详见 `需求描述.md` Phase 5 详细规划。
2. **多语言 UI 本地化支持 (I18n)**: 按照系统本地的语言（Locale）做显示。
3. **更多查询维度的支持**: 增加指令卡性能、宝具 NP 回收等进阶硬核数值查询。

## 活跃问题 & 注意事项

- **macOS pip 外部管理**: 需使用 `python3 -m venv .venv` 创建虚拟环境
- **昵称被 LLM 改写导致搜不到从者**: 双层防护已实施（Prompt 约束 + 归一化匹配）

## ADR 索引

> 详情见 `docs/adr/ADR-NNN-*.md`，仅在涉及相关架构时按需阅读。

| # | 标题 | 日期 | 状态 |
|:--|:-----|:-----|:-----|
| 001 | [FGO NP 数据精度约定](docs/adr/ADR-001-fgo-np-data-precision.md) | 2026-05-05 | 已采纳 |
| 002 | [数据获取方式](docs/adr/ADR-002-data-fetching-strategy.md) | 2026-05-05 | 已采纳 |
| 003 | [AI Native 架构](docs/adr/ADR-003-ai-native-architecture.md) | 2026-05-05 | 已采纳 |
| 004 | [Schema Mirror — 知识提取](docs/adr/ADR-004-schema-mirror.md) | 2026-05-05 | 已采纳 |
| 005 | [全链路日志追踪](docs/adr/ADR-005-structured-logging.md) | 2026-05-05 | 已采纳 |
| 006 | [数据后端预消化](docs/adr/ADR-006-pre-digestion.md) | 2026-05-05 | 已采纳 |
| 007 | [LLM Contract — JSON Schema + Pydantic](docs/adr/ADR-007-llm-contract.md) | 2026-05-05 | 已采纳 |
| 008 | [Phase 5 优先级重排](docs/adr/ADR-008-phase5-priority.md) | 2026-05-06 | 已采纳 |
| 009 | [Filter Registry 模式](docs/adr/ADR-009-filter-registry.md) | 2026-05-06 | 已采纳 |
| 010 | [知识与配置物理分离](docs/adr/ADR-010-knowledge-config-separation.md) | 2026-05-06 | 已采纳 |
| 011 | [Chaldea 依赖边界](docs/adr/ADR-011-chaldea-dependency-boundary.md) | 2026-05-06 | 已采纳 |
| 012 | [LLM 迁移至 Responses API](docs/adr/ADR-012-responses-api-migration.md) | 2026-05-06 | 已采纳 |
| 013 | [Thinking Steps SSE](docs/adr/ADR-013-thinking-steps-sse.md) | 2026-05-06 | 已采纳 |

## 技术备忘

- **LLM 意图解析链路**: 用户输入 → LLM 解析为 JSON 指令 → Query Executor 执行 → LLM 格式化结果 → 返回对话框
- **数据精度**: FGO NP 值以 1/10000 为单位存储，`Value=3000` 表示 30%
- **Atlas Academy 批量端点**: `https://api.atlasacademy.io/export/JP/nice_servant_lang_en.json`
- **Schema Mirror 知识源**: Chaldea `effect.dart` (SkillEffect 40+分类)、`func.dart` (FuncType 165+)、`buff.dart` (BuffType 200+)、`common.dart` (SvtClass 50+)
- **Chaldea 关键数据路径**: `servant.skills[] → skill.functions[] → function.svals[9].Value` (Lv.10数值)
- **效果分类体系**: 攻击(20种) / 防御(11种) / 异常(15种) / 辅助(9种) = 55+ 子分类
- **测试命令**: 默认回归测试使用 `.venv/bin/python -m pytest`；真实 LLM smoke test 使用 `RUN_LIVE_LLM_TESTS=1 .venv/bin/python -m pytest tests/test_llm_client_live.py -s`