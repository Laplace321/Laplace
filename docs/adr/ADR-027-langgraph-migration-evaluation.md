# ADR-027: Pipeline A 架构 vs LangGraph 迁移评估

**状态**: 已评估（不迁移 LangGraph，改为自研声明式图引擎）  
**日期**: 2026-06-12  
**决策者**: 羽殊  
**后续**: 自研方案的详细设计见 ADR-028

## 背景

在设计多轮对话状态模块（ADR-025）时，需要评估是否应将 Pipeline A 的 skill-based 命令式架构迁移到 LangChain/LangGraph 的声明式状态图架构。

## 评估结论

**不建议全面迁移到 LangChain/LangGraph。** 建议借鉴 LangGraph 的状态管理理念，在现有架构上增量实现多轮状态。

## 逐维度对比

### 1. 状态管理（LangGraph 有结构性优势）

- 当前：局部变量传递，无跨轮持久化，无节点级回退
- LangGraph：类型化 State dict + Checkpointer（Memory/Redis），内置状态快照和回放
- **但**：Laplace 可用轻量方案（内存 Session + TurnState dataclass，~300 行）实现同等效果

### 2. 技能系统（LangGraph 无帮助）

- 25 个技能 + SKILL_REGISTRY + 域分组 AND 合并 + 4 级降级是 Laplace 核心领域抽象
- LangGraph 不提供任何等价能力，迁移后这部分代码必须保留
- 技能执行器只能被包装为一个 LangGraph 节点，无法利用图结构简化

### 3. LLM 容灾（当前实现更优）

- 自研两层降级（同供应商多模型 → 跨供应商）+ 6 类错误分类 + rate_limit 特殊处理
- LangChain 的 `with_fallbacks()` 粒度不够，无法表达 skip-provider 级别的降级逻辑
- 迁移后需保留或重写 provider.py（569 行），不是"免费"获得的

### 4. 人机交互/澄清（LangGraph 有优势）

- LangGraph 的 `interrupt_before`/`interrupt_after` 天然适合澄清场景
- 当前的 confirmation_context 拼装 + 直通路径是硬编码特殊分支
- **但**：改进当前实现的成本也不高

### 5. SSE 流式输出（需要适配层）

- 当前 6 种业务事件类型（thinking/servants/clarification/delta/done/error）是领域特定的
- LangGraph 的 `astream_events()` 是通用事件流，需要适配层转换

### 6. 依赖风险（高）

- 当前项目 <15 个直接依赖，引入 LangChain 生态增加 80+ 传递依赖
- LangChain 以频繁 breaking changes 著称（v0.1 → v0.2 → v0.3 大量 API 变动）

## 迁移成本估算

| 模块 | 行数 | 迁移策略 | 工作量 |
|:---|:---|:---|:---|
| pipeline.py 路由+降级+SSE | ~1500 行 | 重写 | 高 |
| main.py 路由层 | ~200 行 | 修改 | 中 |
| llm/provider.py | 569 行 | 保留或适配 | 中 |
| skills/ 全部 | ~3700 行 | 保留 | 零 |
| prompts.py + context_builder.py | ~900 行 | 保留 | 零 |

约 1500 行重写 + 1000 行适配 vs 轻量方案约 300 行新增。

## 决策理由

1. 收益集中在状态管理，可用轻量方案实现
2. 核心竞争力（技能系统、LLM 容灾）与 LangGraph 正交
3. 重量级依赖风险大于收益
4. 当前架构已是 Harness Agent 模式，与 LangGraph 设计哲学一致，仅实现形式不同（命令式 vs 声明式）

## 替代方案

在现有架构上增量实现多轮状态（详见 ADR-025）：

```python
@dataclass
class TurnState:
    pipeline: str
    skill_calls: list[SkillCall]
    response_skill: str
    execution_result: ExecutionResult
    query_summary: str

class SessionState:
    session_id: str
    turns: list[TurnState]
    created_at: float
    last_active: float
```

改动集中在 pipeline.py（注入上轮摘要 + MINOR 复用）和 prompts.py（分类器扩展），保持所有其他模块不变。
