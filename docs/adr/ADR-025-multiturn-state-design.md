# ADR-025: 多轮对话状态模块设计调研

**状态**: 调研中  
**日期**: 2026-06-12  
**决策者**: 羽殊

## 背景

Laplace 后端当前完全无状态，每次 `/chat` 或 `/chat/stream` 请求独立处理。`ChatRequest` 中没有 `session_id` 或历史消息字段，唯一的状态延续是 `confirmation_context`（澄清选择后直通查库，跳过路由）。前端 `app.js` 在 localStorage 维护会话列表，但不向后端发送对话历史。三条管线每次都从 Stage 0 分类器重新开始。

### 核心问题

- 用户说"其中有哪些是弓阶的？"时，系统完全不知道"其中"指代什么，体验断裂
- 用户得到不正确/不完整回复后，补充或纠正问题时系统无法识别为继续对话
- 产品从单轮工具向对话式助手演进的关键能力缺失

### 参考来源

参考 AI-DATA (NL2Data) 平台架构文档中的**有向状态图**和 **MINOR/MAJOR 意图分类**设计（详见 ADR-026）。

## 设计方向

### MINOR/MAJOR 意图分类

借鉴 AI-DATA 的核心模式，将用户的每一轮输入分类为：

```
MINOR（追问/补充/纠正）：
  - "其中有哪些是弓阶的？"（在上一轮结果上追加过滤）
  - "只看5星的"（缩小范围）
  - "我说的是FGO的伊织"（纠正上一轮的歧义）
  → 不需要重新路由，复用上一轮的技能链 + 查询结果

MAJOR（新问题）：
  - "顺便查一下黑贞的技能"（切换查询对象）
  - "剑阶戴冠怎么配队？"（切换管线）
  → 清空上轮状态，重新走完整链路
```

### 节点级回退

对应 Laplace Pipeline A 的状态节点：

```
[分类] → [路由] → [技能执行] → [上下文构建] → [回复生成]

MINOR 追问时的回退策略：
  "其中弓阶的"        → 回退到 [技能执行]，追加 search_by_class 过滤
  "换个详细点的回复"   → 回退到 [回复生成]，切换 response_skill
  "我说的是Alter版本"  → 回退到 [路由]，修正名字参数
```

## 关键决策点（待定）

### 决策 1：状态存储位置

| 方案 | 优势 | 劣势 |
|:---|:---|:---|
| **前端传入对话历史** | 后端保持无状态，水平扩展无障碍 | 每次请求 payload 增大 |
| **后端 Session 存储** | 后端完全控制状态，支持复杂回退 | 引入存储依赖，需 session 清理 |
| **混合方案** | 平衡 payload 与能力 | 实现复杂度较高 |

当前单实例部署（Docker + uvicorn），后端内存存储（带 TTL 自动清理）是最轻量的起步方案。

### 决策 2：MINOR/MAJOR 分类实现

扩展现有 Stage 0 分类器，将上一轮摘要注入分类器 prompt：

```json
{
  "pipeline": "A/B/C",
  "turn_type": "MAJOR/MINOR/CORRECTION",
  "reason": "...",
  "tags": [...]
}
```

### 决策 3：MINOR 时复用粒度

| 粒度 | 场景 | 复杂度 |
|:---|:---|:---|
| 复用管线分类 | "再帮我看看宝具效果" → 仍走 Pipeline A | 低 |
| 复用技能路由 + 追加过滤 | "其中弓阶的" → 复用上轮 SkillCall + 追加 search_by_class | 中 |
| 复用查询结果 + 重新生成 | "换个详细的回复" → 复用数据，切换 response_skill | 低 |
| 部分修正路由参数 | "我说的是Alter版" → 修正 lookup_servant 的 name 参数 | 高 |

## 与 AI-DATA 的关键差异

1. **链路长度不同**：AI-DATA 的 SQL 链路很长（选表→选字段→生成SQL→校验），回退收益大；Laplace Pipeline A 链路较短，回退场景相对少
2. **多管线复杂性**：AI-DATA 只有一条 SQL 链路；Laplace 有三条管线，追问可能跨管线（如 A→C："刚才那个从者在戴冠战里好用吗？"）
3. **结果形态不同**：AI-DATA 返回表格数据，可直接追加过滤；Laplace 返回文本+卡片，需保留结构化查询结果
4. **状态一致性风险**：AI-DATA 踩过的坑——MINOR/MAJOR 标记位在多分支场景下容易残留，所有触发新问题的路径必须显式清除上轮状态

## 最小可用方案（建议）

1. `ChatRequest` 增加 `session_id` + `history`（最近 2-3 轮摘要）
2. Stage 0 分类器扩展 MINOR/MAJOR 判断
3. MINOR 时复用上一轮的管线分类和技能路由结果，只追加/修正参数
4. 改动集中在 `pipeline.py` 和 `prompts.py`，不引入新存储依赖

## 关联议题：未识别特性时的主动澄清（待实施时讨论）

### 背景

灵衣特性识别的修复（trait_aliases.json + prompts.py 路由提示）解决了**已知别名**的口语变体覆盖。但**未知特性词**仍存在 Agent 兜底幻觉风险：

- 用户问"有 XX 特性的从者"，但 XX 不在 `trait_aliases.json` / `func_target_types.json` 任何映射中
- Pipeline A 路由失败 → 落到 Agent 兜底
- Agent 调用 `search_servants` 时**未传任何特征筛选参数**（traitNames/effectNames 全空）
- LLM 仍可能生成"按 XX 特性筛选出以下从者"的回复 → 数据污染、违反 SOUL 第 6 条 Strict JSON 与"LLM 不直接操作数据"

### 设计方向（与多轮状态强耦合）

这本质上是 **CORRECTION/CLARIFICATION 类回合** 的一种特化场景，应纳入本 ADR 的状态机统一设计：

```
[识别失败检测]
  └─ 触发条件：search_servants 入参 traitNames/effectNames 全空
              且 用户问句中存在明显特性词（启发式："XX 特性"/"XX 持有者"/"有 XX 的"）
  └─ 输出：CLARIFICATION 回合
          - 不输出筛选结果
          - 用 "未能识别该特性 XX，可否用其他说法描述？"
          - 列出最近匹配的 N 个候选特性（基于 trait_aliases.json 的 fuzzy match）

[用户补充答复] → MINOR 回合（CORRECTION 子类型）
  └─ 复用上一轮 skill_call 框架，仅替换 traitNames
  └─ 进入正常路由
```

### 关键决策点（待实施时确认）

1. **检测位置**：放在 Agent 兜底前（路由层判定）还是 Agent 内部（工具调用 hook）？
   - 路由层：覆盖率高但需要在 prompts.py 加复杂 guardrail
   - Agent 内部：通过 search_servants 入参校验拦截，逻辑集中但可能漏掉非 Agent 路径

2. **"明显特性词"的判定**：纯启发式（关键词后缀）vs 调用 LLM 二次判定？前者快但召回受限

3. **候选项推荐**：是否引入轻量级编辑距离/向量召回，给用户列 3-5 个最相近的已知特性？需评估冷启动质量

4. **与 MINOR/MAJOR 的关系**：CLARIFICATION 应作为 MINOR 的子类型还是独立回合类型？影响 Stage 0 分类器的 schema

### 不立即实施的原因

- 当前没有多轮 session 存储基础设施，澄清后的补答无法回到"上一轮 skill_call 框架"复用
- 必须先完成本 ADR 的最小可用方案（session_id + history），再叠加这一层防幻觉
- **暂行兜底**：通过 prompts.py 第 12 条的明确表述（如"灵衣"边界说明）和 trait_aliases.json 的别名扩展，覆盖**已知**口语变体；未知词的 Agent 防幻觉留待本 ADR 实施时统一解决
