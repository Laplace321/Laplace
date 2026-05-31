# ADR-024: 两阶段路由架构

**状态**: 已实施  
**日期**: 2026-05-31  
**决策者**: 羽殊

## 背景

当前路由采用单体 Prompt（~19k chars / ~6k tokens），在一次 LLM 调用中同时完成链路分发（A/B/C）、Skill 选择和参数提取。随着链路 B/C 的规则不断增加，Prompt 持续膨胀，规则互相干扰导致误路由频发。

### 核心问题

- **规则冲突**：链路 C 的"推荐"关键词与链路 A 的筛选查询重叠（trace 456f00c3：「剑阶出星推荐」被误判为攻略推荐）
- **Prompt 膨胀**：每修复一个误路由 case 就需要新增排除条件，本质上在不断叠加 Prompt，长期不可取
- **职责耦合**：链路分发（三分类）和参数提取（复杂结构化输出）耦合在同一个 Prompt 中

## 决策

将单体路由拆分为两阶段：

### Stage 0 — 链路分类器

- **职责**：只判断 A/B/C 链路 + 输出置信度
- **Prompt**：极简（~1.8k chars），只描述三条链路的边界定义 + 消歧义规则
- **输出**：`{"pipeline": "A"|"B"|"C", "confidence": 0.0-1.0}`
- **模型**：先用同一模型验证架构可行性，后续可切快速/廉价模型

### Stage 1 — 参数提取器（仅链路 A）

- **职责**：Skill 选择 + 参数提取 + 歧义检测
- **Prompt**：从 ~19k 精简至 ~15k chars（删除所有链路 B/C 分发规则）
- **触发条件**：仅当 Stage 0 判定为链路 A 且置信度 ≥ 0.6 时调用

### 分发逻辑

```
用户查询 → Stage 0 分类器
  ├── pipeline=B → _handle_atlas_pipeline()（内部独立 LLM 提取 atlas_query）
  ├── pipeline=C → _handle_guide_pipeline()
  ├── pipeline=A, confidence≥0.6 → Stage 1 参数提取 → SkillExecutor
  └── pipeline=A, confidence<0.6 → Agent fallback
```

## 实施细节

### Schema 层（server/schemas.py）

- 新增 `ClassifierResponse` Pydantic 模型（pipeline + confidence）
- 新增 `parse_classifier_response()` 解析函数
- 新增 `classifier_response_json_schema()` 函数

### Prompt 层（server/prompts.py）

- 新增 `build_classifier_prompt()` 函数
- `build_routing_prompt()` 精简：删除规则 17（戴冠战→C）、20（Atlas→B）、21（攻略→C）及 target_pipeline/atlas_query 相关说明

### Pipeline 层（server/pipeline.py）

- `handle_skill_mode()` 和 `stream_event_generator()` 在 Stage 1 前插入 Stage 0 调用
- `_handle_atlas_pipeline()` 新增独立 LLM 参数提取（`_extract_atlas_query()`）
- 新增 `classifier_output` 和 `atlas_query_extraction` 日志 trace phase
- 删除 Stage 1 中残留的 target_pipeline B/C 分发代码

### 测试层

- 新增 `tests/test_classifier.py`：39 个测试用例
- 修改 `tests/test_skill_api.py`：mock 适配两阶段调用
- 新增 `tests/test_schemas.py`：ClassifierResponse 相关测试

## 后续演进

1. **Stage 0 模型切换**：架构验证可行后，可将 Stage 0 切换为更快/更廉价的模型（如 Qwen-turbo），降低分类延迟和成本
2. **置信度阈值调优**：根据线上数据调整 0.6 的 fallback 阈值
3. **链路 B 参数提取优化**：当前独立 LLM 调用提取 atlas_query，后续可考虑规则化提取减少 LLM 调用

## 参考

- 架构讨论文档：`docs/architecture-discussions/two-stage-routing.md`
- ADR-022：A→B→C 三级知识问答链路架构
