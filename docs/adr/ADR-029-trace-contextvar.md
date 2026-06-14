# ADR-029: 全链路 Trace 协程级传播 + Phase 常量化 + with_trace 装饰器收口

**状态**: 已实施
**日期**: 2026-06-15
**决策者**: Laplace
**关联 ADR**: ADR-005（结构化日志）、ADR-028（声明式管线 + 11 节点 DAG）

---

## 一、背景

ADR-005 时代的 trace_id 完全靠 `PipelineState.trace_id` **显式传递**：每个节点函数都要手写 `log_trace_event(trace_id=state.trace_id, ...)`，每个 LLM 调用、Skill 调用都要把 `trace_id` 当参数透传。在 v0.5.0 自研 DAG 引擎落地（11 节点 + StateGraph）后，调研发现 10 处盲区，其中 4 处与 trace 体系直接相关：

1. **ContextVar 缺失**：协程切换时 trace_id 必须靠 state 传，深层调用（LLM client、Skill executor）需要逐层透传，遗漏一处即丢失
2. **节点埋点不一致**：classify 仅 1 个事件 vs generate 拆 4 个，phase 名称、字段、粒度全部各异
3. **with_trace 装饰器空置**：[server/graph/decorators.py](file:///Users/laplace/Laplace/server/graph/decorators.py) 已定义但从未被任何节点引用
4. **新旧模式混用**：`log_chat_trace_async`（旧）与 `log_trace_event`（新）并存，phase 字符串硬编码

## 二、决策

### 2.1 ContextVar 自动传播

```python
# server/logger.py
current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)

def bind_trace_id(trace_id: str) -> None:
    current_trace_id.set(trace_id)

def get_trace_id() -> str:
    return current_trace_id.get() or "unknown"
```

- 所有入口（`handle_skill_mode` / `stream_chat_events` / `_stream_confirmation_direct` / `_stream_preset`）必须先调 `bind_trace_id(state.trace_id)` 再启动 DAG
- `log_trace_event` 的 `trace_id` 参数变 Optional，缺省时自动从 ContextVar 读
- LLM client / Skill executor / 监控指标 不再需要透传 trace_id

### 2.2 Phase 常量化

```python
class Phase:
    ROUTING_INPUT = "routing_input"
    ROUTING_OUTPUT = "routing_output"
    CLASSIFIER_OUTPUT = "classifier_output"
    NODE_CLASSIFY = "node_classify"
    NODE_EXECUTE = "node_execute"
    NODE_GENERATE = "node_generate"
    # ... 共 30+ 常量

PHASES: frozenset[str] = frozenset({...})  # 全集

def validate_phase(phase: str) -> None:
    if phase not in PHASES:
        raise ValueError(f"unknown phase: {phase}")
```

- 杜绝 phase 字符串拼写漂移
- 新加 phase 必须先扩 `Phase` 类 + `PHASES` frozenset，单测 `tests/test_logger_phase_constants.py` 强制约束

### 2.3 with_trace 装饰器升级

```python
# server/graph/decorators.py
@with_trace("node_classify")
async def classify_node(state: PipelineState) -> PipelineState:
    ...
    return state
```

装饰器自动完成：
- 从 ContextVar 读 trace_id
- 入口埋 `{phase}_input` 事件（携带 `node_name` / `state_summary`）
- 出口埋 `{phase}_output` 事件（携带 `latency_ms` / `result` / `metric_labels` 切片）
- 异常分支埋 `{phase}_error` 事件并 re-raise（保留原异常）
- 出口同步调 `monitor.metrics.record_node_latency(node_name, result, latency_ms)`

**收益**：节点函数代码量减少约 30%，埋点 schema 100% 一致。

### 2.4 删除旧 API

- 删除 `log_chat_trace_async` / `log_chat_trace`（grep 全仓确认无引用）
- 删除散落的显式 `log_trace_event` 调用（被装饰器覆盖的部分），保留装饰器无法覆盖的中间阶段（如 generate 的 `context_build`）但改用 Phase 常量

## 三、实施情况（已完成）

- [server/logger.py](file:///Users/laplace/Laplace/server/logger.py)：ContextVar / Phase / validate_phase / `log_trace_event` 改造完成
- [server/graph/decorators.py](file:///Users/laplace/Laplace/server/graph/decorators.py)：`with_trace` 升级标准化 schema
- 11 节点全部接入 `@with_trace`：classify / route / execute / generate / guide / clarify / atlas_search / fact_verify / merge_filters / template_fallback / session_save
- [server/main.py](file:///Users/laplace/Laplace/server/main.py)、[server/pipeline.py](file:///Users/laplace/Laplace/server/pipeline.py)：入口处 `bind_trace_id(trace_id)` 已加
- 旧 API 已删除，全仓 grep 干净
- 测试覆盖：[tests/test_logger_async.py](file:///Users/laplace/Laplace/tests/test_logger_async.py) 补 ContextVar 传播 + 装饰器异常路径

## 四、风险与回退

- **ContextVar 在 BackgroundTasks 中失效**：FastAPI BackgroundTasks 默认继承 ContextVar；若发现失效，在调度处显式 `copy_context()`
- **回退路径**：`bind_trace_id` 是单点开关，关闭后旧的 `log_trace_event(trace_id=...)` 显式调用仍可工作；with_trace 是叠加装饰器，单独 revert 不破坏节点签名

## 五、与 ADR-005 的关系

ADR-005 仍是 trace 体系的总纲（JSONL 格式、按 trace_id 检索），本 ADR 只是在其上补充：

- 补「自动传播」机制（ContextVar）
- 补「字段一致性」机制（Phase 常量 + with_trace schema）
- 补「实现卫生」机制（删除旧 API、删除冗余显式埋点）
