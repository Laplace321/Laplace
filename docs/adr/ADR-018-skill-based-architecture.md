# ADR-018: Skill-Based Architecture 架构决策

- **日期**: 2026-05-07
- **状态**: 已决策 (Decided)
- **参与者**: 用户（羽殊）、AI Agent
- **前置讨论**: [ADR-013 Skill 架构与 AI 技术方案讨论](../architecture-discussions/ADR-013-skill-architecture.md)
- **触发问题**: 单一扁平 intent 架构的扩展性瓶颈

---

## 一、问题背景

### 1.1 当前架构痛点

当前查询架构是单一管线：`intent="query_servants"` + 20+ 个扁平 `QueryConditions` 字段。

**核心问题**：
- **Token 浪费**：LLM 每次面对全部 20+ 字段的 System Prompt（~2000 Token），即使用户只问一个简单的职阶筛选
- **幻觉概率高**：字段越多，LLM 误填/混填的概率越大
- **能力边界不透明**：新增查询能力需要修改 4 个文件（prompts.py, schemas.py, query_executor.py, main.py），耦合严重
- **回复格式单一**：无论查询类型如何，都使用同一套 RAG Prompt 生成回复，无法针对对比/详情/列表等场景定制

### 1.2 从 ADR-013 到 ADR-018 的演进

ADR-013 讨论了 Skill 架构的初步方案（轻量级 Skill 系统 vs 完整 MCP），确定了"轻量级 Skill 系统"的方向。本 ADR 在此基础上深化讨论了八项关键决策。

---

## 二、决策讨论过程

### 决策 1：Skill 的粒度如何定义？

**讨论了三种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 按数据域拆分** | 每个数据域一个 Skill（query_servants、query_craft_essences） | 改动最小，Filter Registry 完全复用 | 单个 Skill 内 conditions 仍是扁平大杂烩，Token 成本不变 |
| **B: 按能力原子拆分** | 每种查询能力独立为 Skill（search_by_np_charge、search_by_skill_effect） | 每个 Skill 参数精简（3-5 字段），LLM 理解成本低、幻觉率低 | 改动最大，复合查询需要 Skill 组合机制 |
| **C: 混合模式** | 按数据域拆 Skill，内部将 conditions 分为 ConditionGroup | 兼顾清晰和实现成本 | 分组规则需要设计，两边优势都打折 |

**结论**：**方案 B — 按能力原子拆分**

**决策理由**：
- 每个 Skill 参数精简（3-5 字段），LLM 理解成本低、幻觉率低、Token 省
- 能力边界完全声明化，新增能力即新增 Skill 文件
- 虽然改动最大，但配合"一步到位重写"策略可以一次性完成

---

### 决策 2：LLM 如何路由到正确的 Skill？

**讨论了三种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 两阶段 LLM** | 第一次调用做 Skill 路由，第二次用专属 Prompt 精填参数 | 每阶段 Prompt 短、准确率高；Skill 专属 few-shot 不污染其他能力 | 多一次 LLM 调用（~1-2秒延迟 + Token 成本） |
| **B: 单阶段 + 注册表注入** | 所有 Skill 描述 + Schema 拼入一个 System Prompt，一次调用完成 | 只一次调用，延迟最低 | Skill 增多后 Prompt 膨胀，选错概率上升 |
| **C: 单阶段 + 动态裁剪** | 用关键词/embedding 预判 Skill 子集，只注入相关 Skill | 一次调用 + Prompt 精简 | 预判规则需维护，可能误判 |

**结论**：**方案 A — 两阶段 LLM**

**决策理由**：
- Stage 1 路由 Prompt 精简（~500 Token，只含 Skill 名 + 描述）
- Stage 2 只注入被选中 Skill 的详细参数 Schema + few-shot（~300-500 Token/Skill）
- 总 Token 成本与现有单阶段相当甚至更低
- Skill 专属 Prompt 可包含特有 few-shot 示例，不污染其他能力
- 更准确，虽然多一次调用

> [!NOTE]
> Token 成本分析：现有单阶段 System Prompt ~2000 Token。两阶段后：Stage 1 ~500 Token + Stage 2 ~500 Token = ~1000 Token，反而更省。

---

### 决策 3：迁移策略 — 是否一步到位？

**讨论了两种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 渐进式迁移** | 先包装现有逻辑为第一个 Skill，后续逐步拆分 | 风险低，每步可验证，不破坏现有 54 个测试 | 中间态代码有"脚手架"感，需多次重构 |
| **B: 一步到位重写** | 一次性设计完整 Skill 体系，重写全部相关模块 | 架构一步到位，无中间态技术债 | 改动量大，回归测试全部重写 |

**结论**：**方案 B — 一步到位重写**

**决策理由**：
- 配合"按能力原子拆分"的粒度决策，渐进式迁移的中间态意义不大
- 现有测试本身就需要重新组织为 Skill 粒度
- 一次性完成避免多次重构的沟通和认知成本

---

### 决策 4：Skill 的定义放在哪里？

**讨论了两种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 代码即定义** | 每个 Skill 是 Python 文件，包含元数据 + 执行函数 + Prompt | IDE 可跳转，类型安全，Pydantic Schema 直接复用 | 非技术人员无法编辑 |
| **B: 配置驱动** | Skill 定义在 JSON/YAML 配置中 | 非技术人员可修改，支持热更新 | 类型不安全，配置与代码脱节 |

**结论**：**方案 A — 代码即定义**

**决策理由**：
- 与现有 Filter Registry 模式一致（`@register_filter` 装饰器）
- 类型安全，Pydantic Schema 直接复用
- IDE 友好，可跳转、可重构
- 本项目无"非技术人员编辑"的需求

---

### 决策 5：多 Skill 组合策略

**问题**：当用户的问题涉及多个 Skill 时（如"30自充的有无敌技能的五星剑阶"），如何组合？

**讨论了三种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: Skill 列表 + AND 合并** | 路由输出 skills 数组，执行层 AND 合并过滤 | 与原子拆分最契合，执行简单 | 跨域组合（从者+礼装）需额外处理 |
| **B: 主 Skill + 附加过滤** | 路由选一个主 Skill，附带通用 filters | LLM 只选一个 Skill，路由准确 | 又回到 filters 大杂烩 |
| **C: Composite Skill** | 复合查询走特殊的 composite_search Skill | 单一/复合场景清晰区分 | composite 本身又成"大 Skill" |

**深入讨论**：用户提出跨域组合的可行性问题。分析发现：

- **同域组合**（从者查询组合多个过滤条件）：AND 合并即可
- **跨域管道**（如"适合XX从者的礼装"）：几乎都是有序管道（先 A 后 B），不是并行 AND

**结论**：**同域 AND 合并 + 跨域管道预留 domain 字段**

**具体设计**：
- 每个 Skill 声明 `domain: str` 属性（如 `"servant"`）
- 同 domain 的 Skills → AND 合并，一次数据扫描
- 不同 domain 的 Skills → 按序管道执行（当前不实现，预留接口）
- 当前只有从者域，只需实现同域 AND 合并

---

### 决策 6：RAG 回复如何抽象？

**问题**：不同查询场景需要不同的回复格式。例如：
- "30自充从者有哪些" → 列表式回复
- "村正的技能是什么" → 详情卡片式回复
- "对比村正和武尊" → 维度对比式回复
- "诸葛孔明适合配什么队伍" → 辅助角色分析式回复

**讨论了三种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 每个原子 Skill 自带 generation_prompt** | 回复模板内嵌在 Query Skill 中 | 自封装 | 多 Skill 组合时，不知道用谁的模板 |
| **B: 通用回复模板** | 一套通用模板适配所有场景 | 实现简单 | 从者对比和单从者分析完全不同，无法通用 |
| **C: Response Skill 独立类型** | 将回复生成独立为 Skill 类型，与 Query Skill 分离 | Query 保持纯粹，Response 标准化，独立演进 | 需要路由阶段同时输出 response_skill |

**深入讨论**：用户指出方案 A 有根本矛盾 — 一个问题通常组合多个原子 Skill，回复该用谁的 generation_prompt？方案 B 完全不可行 — 从者对比、单从者分析、辅助角色分析的回复维度完全不同。

**结论**：**方案 C — Query Skill + Response Skill 双类型分离**

**具体设计**：

```
Skill 分为两类：
├── Query Skill（查询型）：负责参数解析 + 数据过滤，不管回复格式
│   ├── search_by_np_charge
│   ├── search_by_skill_effect
│   ├── search_by_class
│   └── ... (共 10 个)
└── Response Skill（回复型）：负责将查询结果组织成特定格式
    ├── respond_servant_list     — 列表式回复
    ├── respond_servant_detail   — 详情卡片式回复
    ├── respond_servant_compare  — 对比分析式回复
    └── respond_support_analysis — 辅助角色分析式回复
```

路由阶段同时输出 `query_skills[]` 和 `response_skill`：

```json
{
  "query_skills": [
    {"name": "search_by_np_charge", "params": {"op": "eq", "value": 30}},
    {"name": "search_by_class", "params": {"className": "saber"}}
  ],
  "response_skill": "respond_servant_list"
}
```

---

### 决策 7：Skill 不支持时的降级策略

**问题**：当用户问了现有 Skill 无法覆盖的问题时（如"樱花好看吗"、"FGO 什么时候开新活动"），数据链路如何流转？

**讨论了三种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 路由阶段兜底** | Stage 1 输出 `fallback` 意图，后端直接返回预设回复 | 快速拦截，不浪费 Stage 2 调用 | 依赖 LLM 准确判断"不属于任何 Skill" |
| **B: 执行阶段兜底** | LLM 照常选 Skill，查询结果为空或参数校验失败时降级 | 不需要路由阶段额外逻辑 | 浪费了 Stage 2 的 LLM 调用 |
| **C: 双重兜底** | 路由阶段先判断 + 执行阶段再兜底 | 两层保护，用户体验最好 | 实现略复杂 |

**结论**：**方案 C — 双重兜底**

**具体设计**：

**第一层（路由阶段）**：Stage 1 路由 Prompt 明确告知 LLM，如果用户问题不属于任何已注册 Skill，输出：

```json
{
  "query_skills": [],
  "response_skill": "fallback",
  "fallback_reason": "non_game_query"
}
```

`fallback_reason` 枚举值：
- `non_game_query` — 非游戏相关问题 → 回复"我是 FGO 助手，暂时无法回答非游戏相关的问题"
- `unsupported_query` — 游戏相关但不支持 → 回复"这个查询能力还在开发中，目前支持的查询有：XXX"
- `clarification_needed` — 问题模糊 → 追问用户明确意图

**第二层（执行阶段）**：
- 查询结果为 0 条 → 生成"未找到匹配结果，您可以尝试调整条件"
- Skill 参数 Pydantic 校验失败 → 降级为通用回复
- Skill 名称不在注册表中 → 返回错误提示

---

### 决策 8：前端交互 — 预设组合 + 自由输入混合模式

**问题**：用户是否可以在前端通过 UI 交互显式选择查询能力，而不完全依赖 LLM 理解自然语言？

**讨论了三种方案**：

| 方案 | 思路 | 优势 | 劣势 |
|:---|:---|:---|:---|
| **A: 纯自由输入** | 用户只能用自然语言提问，完全依赖 LLM 路由 | 交互最简单 | 每次都消耗 Stage 1 LLM 调用 |
| **B: 纯预设组合** | 用户只能从预设的查询套餐中选择 | 零 LLM 路由成本 | 灵活性差，无法覆盖长尾场景 |
| **C: 混合模式** | 预设组合快捷入口 + 自由输入，快捷入口跳过 Stage 1 | 高频场景零延迟，长尾场景有兜底 | 需要前端 + 后端同时开发 |

**结论**：**方案 C — 混合模式（预设组合 + 自由输入），当前阶段同时实现后端接口 + 前端 UI**

**具体设计**：

**后端支持两种调用模式**：

```json
// 模式 1：自由输入（走两阶段 LLM）
{
  "mode": "natural_language",
  "message": "30自充的五星剑阶"
}

// 模式 2：快捷入口（跳过 Stage 1，直接执行或仅走 Stage 2 补充解析）
{
  "mode": "preset",
  "preset_name": "cycle_farming",
  "params": {
    "search_by_np_charge": {"op": "gte", "value": 30}
  },
  "supplement": "有无敌技能的",
  "response_skill": "respond_servant_list"
}
```

**preset 模式的补充描述处理（B1 策略）**：
- `supplement` 为空 → 表单参数直接实例化 Query Skill，完全跳过 Stage 1 和 Stage 2，Token 成本最低
- `supplement` 非空 → 表单参数固定为确定的 Skill 调用（无需 LLM），补充文字仅送 Stage 2 解析出额外 Skill（如 "有无敌技能的" → search_by_skill_effect），然后合并所有 Skills 执行。Stage 1 路由仍然跳过，只多一次 Stage 2 调用

**前端 UI**：输入框上方展示快捷查询标签，选择后展开参数表单 + 补充描述输入框：

```
┌────────────────────────────────────────────┐
│ 快捷查询：                                  │
│ [周回筛选✓] [从者对比] [辅助推荐] [从者查询]  │
│                                            │
│ ┌── 周回筛选 ──────────────────────┐        │
│ │ 充能量：[30 ▼]  条件：[≥ ▼]     │        │
│ │ 职阶：  [全部 ▼]                 │        │
│ │ 星级：  [全部 ▼]                 │        │
│ │                                  │        │
│ │ 补充描述（可选）：                │        │
│ │ [有无敌技能的________________]   │        │
│ │                     [查询]       │        │
│ └──────────────────────────────────┘        │
└────────────────────────────────────────────┘
```

**预设组合清单**（初始版本）：

| 预设名称 | 面向用户名称 | 包含 Skills | Response Skill |
|:---|:---|:---|:---|
| `cycle_farming` | 周回筛选 | search_by_np_charge + search_by_class + search_by_rarity | respond_servant_list |
| `servant_compare` | 从者对比 | compare_servants | respond_servant_compare |
| `support_recommend` | 辅助推荐 | search_by_skill_effect(support) | respond_support_analysis |
| `servant_lookup` | 从者查询 | lookup_servant | respond_servant_detail |

**Token 成本优势**：快捷入口模式完全跳过 Stage 1 LLM 路由，每次请求节省 ~500 Token + ~1 秒延迟。

---

## 三、最终架构设计

### 3.1 决策摘要

| 决策点 | 结论 |
|:---|:---|
| Skill 粒度 | 按能力原子拆分（每种查询能力独立为一个 Skill） |
| LLM 路由 | 两阶段 LLM（Stage 1 路由选 Skill → Stage 2 精填参数） |
| 迁移策略 | 一步到位重写 |
| Skill 定义位置 | 代码即定义（Python 文件，类型安全） |
| 多 Skill 组合 | 同域 AND 合并 + 跨域管道预留 domain 字段 |
| RAG 回复 | Query Skill（纯过滤）+ Response Skill（回复模板）双类型分离 |
| 降级策略 | 双重兜底（路由 fallback + 执行阶段兜底） |
| 前端交互 | 预设组合快捷入口 + 自由输入混合模式 |

### 3.2 数据流

**路径 A：自由输入（natural_language 模式）**

```
用户自然语言
    ↓
Stage 1 LLM: Skill 路由
    输入：用户问题 + Skill 名称/描述列表（~500 Token）
    输出：query_skills[] + response_skill + fallback_reason?
    ↓
    ├─ [fallback] → 降级回复（第一层兜底）
    │     non_game_query → "我是 FGO 助手，暂时无法回答非游戏相关的问题"
    │     unsupported_query → "这个查询能力还在开发中，目前支持：XXX"
    │     clarification_needed → 追问用户明确意图
    │
    └─ [正常] → Stage 2 LLM: 精填参数
                  输入：用户问题 + 被选中 Skill 的专属 Prompt + few-shot（~500 Token）
                  输出：每个 Skill 的精确参数
                  ↓
                  Skill Executor: AND 合并执行
                      同 domain Skills 合并过滤，一次数据扫描
                      ↓
                      ├─ [结果为空/参数校验失败] → 降级回复（第二层兜底）
                      │     结果为 0 → "未找到匹配结果，您可以尝试调整条件"
                      │     Pydantic 校验失败 → 降级为通用回复
                      │     Skill 名不在注册表 → 错误提示
                      │
                      └─ [正常] → Response Skill: 生成回复
                                    使用专属 generation_prompt 模板
                                    ↓
                                  返回结构化结果 + 自然语言回复
```

**路径 B：快捷入口（preset 模式）**

```
用户点击前端快捷标签 + 填写参数 + 可选补充描述
    ↓
跳过 Stage 1（节省 ~500 Token + ~1 秒延迟）
    ↓
    ├─ [supplement 为空] → 表单参数直接实例化 Query Skills
    │     完全跳过 Stage 1 + Stage 2，Token 最低
    │
    └─ [supplement 非空] → 表单参数固定为确定的 Skill 调用
                            + 补充文字仅送 Stage 2 解析出额外 Skills
                            Stage 1 仍然跳过，只多一次 Stage 2 调用
    ↓
合并所有 Skills → Skill Executor: AND 合并执行
    ↓
Response Skill: 生成回复
    ↓
返回结构化结果 + 自然语言回复
```

### 3.3 目录结构

```
server/
├── skills/
│   ├── __init__.py          # 自动导入所有 query/ 和 response/ 模块触发注册
│   ├── base.py              # BaseSkill / QuerySkill / ResponseSkill 基类 + SKILL_REGISTRY
│   ├── executor.py          # SkillExecutor（domain 分组 + AND 合并 + fallback 兜底）
│   ├── presets.py           # 预设组合定义（PRESET_REGISTRY: name → skills + params 模板）
│   ├── query/               # Query Skills（10 个）
│   │   ├── __init__.py
│   │   ├── search_by_np_charge.py
│   │   ├── search_by_skill_effect.py
│   │   ├── search_by_np_effect.py
│   │   ├── search_by_class.py
│   │   ├── search_by_rarity.py
│   │   ├── search_by_traits.py
│   │   ├── search_by_cards.py
│   │   ├── search_by_attribute.py
│   │   ├── lookup_servant.py
│   │   └── compare_servants.py
│   └── response/            # Response Skills（4 个）
│       ├── __init__.py
│       ├── respond_servant_list.py
│       ├── respond_servant_detail.py
│       ├── respond_servant_compare.py
│       └── respond_support_analysis.py
├── schemas.py               # 重写：RoutingResponse + SkillCall + FallbackReason
├── prompts.py               # 重写：build_routing_prompt + build_params_prompt
├── query_executor.py        # 精简：只保留 load_database / load_nicknames / _normalize_text / _match_effect / _compare 等工具函数
└── main.py                  # 重写：双模式入口（natural_language 三阶段 / preset 直接执行）+ fallback 处理
demo/
└── app.js                   # 新增：快捷查询标签 UI + preset 模式 API 调用
```

### 3.4 Skill 清单

**Query Skills（10 个）**：

| Skill | Params Schema | 参数说明 | 迁移自 |
|:---|:---|:---|:---|
| `search_by_np_charge` | `op: CompareOp, value: int` | op 取值 eq/gte/lte/gt/lt；value 为百分比（如 30 表示 30%）；eq 匹配单条充能记录的 chargePercent，gte/lte/gt/lt 匹配 totalCharge | `_filter_np_charge` |
| `search_by_skill_effect` | `effects: list[str], effects_op: "and"\|"or" = "and", target_type: "self"\|"party"\|"enemy"\|None = None` | effects 为效果名数组（如 ["invincible", "guts"]）；effects_op 控制多效果逻辑关系；target_type 筛选效果目标 | `_filter_skill_effect` + `_filter_skill_effects` |
| `search_by_np_effect` | `effects: list[str], effects_op: "and"\|"or" = "and"` | effects 为宝具效果名数组；effects_op 控制逻辑关系；匹配从者 npEffects 字段 | `_filter_np_effect` + `_filter_np_effects` |
| `search_by_class` | `class_name: str` | 小写英文职阶名，取值：saber, archer, lancer, rider, caster, assassin, berserker, ruler, avenger, moonCancer, alterEgo, foreigner, pretender | `_filter_class` |
| `search_by_rarity` | `op: CompareOp, value: int` | op 取值 eq/gte/lte/gt/lt；value 为星级（0-5） | `_filter_rarity` |
| `search_by_traits` | `traits: list[int], exclude_traits: list[int] = []` | traits 为必须拥有的特性 ID 数组（如 [300, 303] = 秩序善）；exclude_traits 为排斥特性 ID；委托 `filter_by_traits()` 执行 | `_filter_traits` |
| `search_by_cards` | `cards: dict[str, int]\|None = None, np_card: "buster"\|"arts"\|"quick"\|None = None, np_target: "one"\|"all"\|"support"\|None = None` | cards 为指令卡数量要求（如 {"buster": 3}）；np_card 为宝具颜色；np_target 为宝具目标类型（one=单体, all=全体, support=辅助） | `_filter_cards`（含 npCard + npTarget） |
| `search_by_attribute` | `gender: "male"\|"female"\|"unknown"\|None = None, attribute: "earth"\|"sky"\|"human"\|"star"\|"beast"\|None = None` | gender 为性别；attribute 为阵营（earth=地, sky=天, human=人, star=星, beast=兽） | `_filter_gender` + `_filter_attribute` |
| `lookup_servant` | `name: str` | 从者名称关键词；执行三级匹配：精确匹配（昵称映射后） → 子串模糊匹配（len >= 2） → 反向子串匹配 | `_filter_name` |
| `compare_servants` | `names: list[str]` | 从者名称数组（2+ 个）；逐个执行 `_filter_name` 匹配，取每个名称的第一个匹配结果，按稀有度降序排序 | `execute_query` 中 names 分支 |

> [!NOTE]
> `CompareOp` 类型定义：`Literal["eq", "gte", "lte", "gt", "lt"]`，复用自现有 `server/schemas.py`。

**Response Skills（4 个）**：

| Skill | 适用场景 | 回复维度 | generation_prompt 核心指令 |
|:---|:---|:---|:---|
| `respond_servant_list` | 筛选类查询（默认） | 列表 + 关键属性高亮 | 报出 total_found 总数；列出 top N 代表从者；按用户查询条件高亮关键数据（如充能值、效果） |
| `respond_servant_detail` | 单从者查询 | 充能/技能效果/宝具/配卡/特性 | 展示从者全属性面板；区分自充/群充/他充；列出技能效果和宝具效果；标注配卡结构和宝具颜色 |
| `respond_servant_compare` | 多从者对比 | 职阶稀有度/NP充能/核心技能/宝具/配卡/综合评价 | 逐维度对比表格或并列结构；突出差异项；给出综合评价和适用场景建议 |
| `respond_support_analysis` | 辅助从者查询 | 辅助能力/充能支援/队伍适配 | 侧重分析辅助能力（群充/攻防 Buff）；给出队伍搭配建议（蓝卡队/红卡队等）；区分纯辅助和兼输出型 |

---

## 四、Token 成本影响分析

> 用户高度关注 Token 成本与性能影响。

| 指标 | 现有架构（单阶段） | 新架构 - 自由输入模式 | 新架构 - preset 模式 |
|:---|:---|:---|:---|
| System Prompt | ~2000 Token（全字段） | Stage 1: ~500 + Stage 2: ~500 | 无 Stage 1/2 |
| LLM 调用次数 | 2 次（意图解析 + RAG 生成） | 3 次（路由 + 参数 + RAG 生成） | 1 次（仅 RAG 生成） |
| 总 Token 估算 | ~3000-4000 Token/请求 | ~2500-3500 Token/请求 | ~1000-1500 Token/请求 |
| 延迟估算 | ~3-4 秒 | ~4-6 秒（+1-2 秒） | ~1-2 秒（最快） |

**结论**：
- **自由输入模式**：Token 成本预计**持平或略降**（每阶段 Prompt 更精简），延迟增加 1-2 秒
- **preset 模式**：Token 成本**降低 60-70%**（跳过 Stage 1 + Stage 2），延迟**降低 50%+**（只有 RAG 生成一次 LLM 调用）
- 高频场景（周回筛选、从者查询）走 preset 模式可显著降低整体成本

---

## 五、风险与缓解

| 风险 | 关联决策 | 缓解措施 |
|:---|:---|:---|
| 一步到位重写改动量大 | 决策 3 | 每个 Skill 独立文件，可逐个实现和测试；现有 filter 函数逻辑直接迁移，降低逻辑出错概率 |
| 两阶段 LLM 路由选错 Skill | 决策 2 | Stage 1 Prompt 精简（~500 Token，只含 Skill 名+描述）；路由测试用例覆盖常见和边界场景 |
| Stage 2 参数填充幻觉 | 决策 2 | 每个 Skill 专属 few-shot 示例（2-3 个）；Pydantic 强校验拦截非法参数 |
| 回归测试全部重写 | 决策 3 | 现有 7 个测试的断言逐条迁移到新 Skill 粒度测试，保持等价覆盖 |
| fallback 路由误判（把合法查询判为 fallback） | 决策 7 | 第二层兜底确保即使路由判错，执行阶段仍会尝试查询；路由 Prompt 中明确列出所有 Skill 的覆盖范围 |
| fallback 路由漏判（把非法查询路由到 Skill） | 决策 7 | 执行阶段 Pydantic 校验 + 结果为空兜底，双重保护 |
| 前后端同步开发增加协调成本 | 决策 8 | 后端先定义 preset API 接口契约（请求/响应 Schema），前端按契约独立开发；preset 模式不依赖 LLM，可独立测试 |
| preset 组合覆盖不足导致用户体验割裂 | 决策 8 | 保留自由输入模式作为兜底；preset 组合根据用户使用数据持续迭代 |

---

## 六、后续行动

- [x] 讨论并确定八项架构决策（Skill 粒度、LLM 路由、迁移策略、定义位置、多 Skill 组合、RAG 回复、降级策略、前端交互）
- [x] 记录架构讨论过程（本文档）
- [ ] 基于本 ADR 生成详细的实施计划（implementation_plan + task.md）
- [ ] 实施 Skill-Based Architecture 重写（后端 Skill 框架 + Query Skills + Response Skills + Schema/Prompt 重写 + main.py 双模式入口）
- [ ] 实施前端快捷查询 UI（demo/app.js 快捷标签 + preset 模式 API 调用）
- [ ] 更新 `需求描述.md` 路线图
- [ ] 更新 `MEMORY.md` 索引

---

**记录人**: AI Agent  
**审核状态**: 已确认  
**优先级**: P0（核心架构升级）
