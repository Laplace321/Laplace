# ADR-023: 链路 A 用户确认机制（两阶段路由）

**状态**: 已实施  
**日期**: 2026-05-26  
**决策者**: 羽殊

## 背景

链路 A（Skill 精确查询）在 LLM 路由阶段直接将用户自然语言解析为 SkillCall 列表。当用户问题存在参数歧义时（如"暴击拐有哪些"未指定 targetType），LLM 只能猜测默认值，可能返回不符合用户预期的结果。

## 决策

在链路 A 的 LLM 路由阶段新增参数完整性检查：LLM 先识别 Skill、提取实体，判断参数是否完整/无歧义；不完整时输出 `clarification` 字段暂停流程，前端展示选项按钮，用户确认后携带选择重新路由。

### 核心机制

1. **RoutingResponse Schema 扩展**：新增 `clarification` 可选字段（`ClarificationRequest` 模型），含 `question`、`options`、`ambiguous_field`
2. **路由 Prompt 规则 22-23**：定义触发/不触发确认的场景，以及 clarification 输出格式
3. **Pipeline 检测**：SSE 和 JSON API 两条路径在路由结果解析后检测 clarification，推送新 SSE 事件类型或返回特殊 ChatResponse
4. **前端交互**：渲染选项按钮 + 自定义输入框，用户选择后携带 `confirmation_context` 参数重新发起请求

### 触发场景

- `targetType` 歧义：效果类查询用户未指定"全队/单体/自身"，且该效果存在多种 targetType
- 从者名多候选：用户提到的名称可能对应多个不同版本从者
- 语义歧义：用户措辞可解读为不同的 Skill 或不同的效果

### 不触发场景（直接用默认值）

- rarity/className/minValue 未指定 → 查全部
- 效果仅有单一合理 targetType（如"无敌"默认自身）
- 用户已明确指定（如"自充"→ self）

## 影响范围

- **仅影响链路 A**（Skill 精确查询），链路 B/C 零改动
- **Token 成本**：无歧义时零额外成本；有歧义时多一次路由调用（约 300-500 tokens）

## 涉及文件

| 文件 | 变更内容 |
|:-----|:---------|
| `server/schemas.py` | 新增 ClarificationOption、ClarificationRequest 模型；RoutingResponse 新增 clarification 字段 |
| `server/prompts.py` | build_routing_prompt() 新增规则 22-23 |
| `server/pipeline.py` | stream_event_generator() 和 handle_skill_mode() 新增 clarification 检测逻辑；新增 confirmation_context 参数 |
| `server/main.py` | ChatRequest 新增 confirmation_context 字段；/api/chat/stream 端点传递参数 |
| `demo/app.js` | handleClarification() 函数 + sendWithConfirmation() 函数 |
| `demo/style.css` | .clarification-* 样式组件 |

## 替代方案

1. **前端硬编码规则**：在前端根据关键词匹配弹出确认。缺点：无法利用 LLM 语义理解，规则维护成本高。
2. **始终询问**：所有查询都先确认。缺点：用户体验差，大部分查询无歧义时多一步交互。
3. **后置确认**：先返回结果再问是否需要细化。缺点：已消耗 Token 和时间，不如前置确认高效。

## 执行层 Clarification（2026-05-27 扩展）

将 Clarification 从路由层扩展到执行层：SkillExecutor 在执行 Skill 后，根据结果（多候选/空结果）生成结构化 clarification 反馈，pipeline 层检测后推送给前端，用户确认后携带选择重新路由。

### 触发场景

| 场景 | 类型常量 | 触发条件 | 处理方式 |
|:-----|:---------|:---------|:---------|
| 名称查询多候选 | `CLARIFICATION_MULTI_CANDIDATE` | `lookup_servant`/`ce_lookup` 单独使用且匹配 >1 个结果 | 直接构建选项列表（id=collectionNo, label=中文名+职阶+星级） |
| 名称查询空结果 | `CLARIFICATION_EMPTY_NAME` | 名称查询 0 结果 | pipeline 层异步调用 `guess_candidates_async()` LLM 猜测候选 |
| 筛选查询空结果 | `CLARIFICATION_EMPTY_FILTER` | 筛选组合 0 结果 | 纯规则生成放宽条件选项（去掉星级/职阶/数值下限等） |

### 架构要点

1. **ExecutionResult 扩展**：新增 `clarification: dict | None` 字段，执行层与 pipeline 层的契约变更
2. **lookup_servant 改造**：从 `filter(bool)` 改为 `execute(list)`，保留所有匹配候选。新增 `find_servant_candidates()` 公开函数供 `compare_servants` 共用
3. **Pipeline 层检测**：SSE 和 JSON API 两条路径在 SkillExecutor 执行完成后、进入 generation 前检测 clarification。名称空结果场景异步调用 LLM 猜测
4. **统一交互流程**：路由层 clarification（`source=routing`）和执行层 clarification（`source=execution`）共享同一个前端 `handleClarification()` + `sendWithConfirmation()` 交互流程

### Token 成本

- 多候选 clarification：零额外 LLM 调用（纯规则匹配）
- 筛选放宽 clarification：零额外 LLM 调用（纯规则生成）
- 名称空结果 LLM 猜测：约 100-200 tokens（复用 resolve_nickname 路径）

### 涉及文件（增量）

| 文件 | 变更内容 |
|:-----|:---------|
| `server/skills/executor.py` | ExecutionResult 新增 clarification 字段；新增类型常量；新增辅助方法（`_is_single_name_lookup`、`_build_multi_candidate_clarification`、`_build_empty_result_clarification`、`_build_filter_relaxation_clarification`、`guess_candidates_async`）；改造 servant/CE domain 执行路径 |
| `server/skills/query/lookup_servant.py` | 从 `filter()` 改为 `execute()`；新增 `find_servant_candidates()` 公开函数 |
| `server/skills/query/compare_servants.py` | 使用共享的 `find_servant_candidates()` |
| `server/pipeline.py` | 两条路径新增执行层 clarification 检测 + LLM 猜测异步调用 |
| `server/logger.py` | `find_trace()` 和 `read_trace_summaries()` 支持 `execution_clarification_requested` phase |
| `demo/style.css` | 多候选列表滚动支持（`max-height` + `overflow-y`） |

## 后续演进

- 可扩展到链路 B/C 的参数确认（如 Atlas 查询时间范围不明确）
- 可记录确认选择作为用户偏好，减少重复确认
- compare_servants 多名称中某个名称多候选时的精确 clarification（当前取星级最高者）
