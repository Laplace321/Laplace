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

## 后续演进

- 可扩展到链路 B/C 的参数确认（如 Atlas 查询时间范围不明确）
- 可记录确认选择作为用户偏好，减少重复确认
