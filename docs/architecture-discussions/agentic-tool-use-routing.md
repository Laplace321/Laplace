# 架构讨论：Agentic Tool Use 路由重构

> 讨论发起日期：2026-05-11
> 状态：讨论中

## 1. 问题陈述

### 1.1 现状

当前路由架构是 **One-Shot Prompt 模式**：

```
用户自然语言 → LLM 一次性输出 SkillCall JSON → SkillExecutor → 结果
               ↑ 300行路由Prompt + 13条规则 + 10个few-shot
               ↑ 必须一次做对，没有纠错机会
```

### 1.2 日志分析暴露的 4 类问题（2026-05-10 ~ 05-11，94 条日志，25 条失败）

| 问题类型 | 失败数 | 根因 | 当前修复方式 |
|:---------|:------|:-----|:-----------|
| 昵称缺失 | 6 | "仇凛"等昵称不在 nicknames.json | 手工加映射 |
| 效果不可查 | 3 | 被动效果不在数据库中，LLM 不知道 | 手工加 Prompt 规则 |
| 特性路由失败 | 2 | LLM 不知道用 search_by_traits | 手工加 Prompt 规则 |
| Prompt 规则膨胀 | - | 规则从 8→13 条，持续增长 | 无解 |

### 1.3 根本矛盾

**LLM 必须一步到位输出精确的结构化指令，没有试错和自我纠正的能力。每个新业务场景都需要人工在 Prompt 里补规则/示例/映射。**

未来要新增礼装查询、关卡数据、素材计算等大量业务场景时，这个模式无法支撑。

---

## 2. 方案对比

### 方案 A：Function Calling 替代 Prompt 路由（之前讨论，已否决）

把 Skill 注册为 function/tool 定义，用 JSON Schema 约束参数。

**为什么不够**：只是换了输出格式，仍然是 one-shot 模式。LLM 仍需一次性正确映射"增伤"→`damageBoost`，没有试错能力。新业务场景仍需手工扩展 function 定义。

### 方案 B：确定性前置层（之前讨论，已否决）

高频查询用正则/模板匹配，长尾才进 LLM。

**为什么不够**：模板脆弱，覆盖率有上限，对新业务场景无帮助。本质上是另一种形式的"穷举打补丁"。

### 方案 C：Agentic Tool Use（推荐方向）

让 LLM 变成一个 **有工具、能试错的 Agent**，通过多轮 tool 调用实现自我纠正。

#### 核心思路

```
用户自然语言
      ↓
┌──────────────────────────────────────┐
│  LLM Agent（带 tools 定义）           │
│                                      │
│  Step 1: 调 list_effects()           │ ← "增伤是什么效果？"
│          → 返回效果列表               │
│  Step 2: 调 search(effect=damageBoost)│ ← "用这个搜"
│          → 返回 442 条结果            │
│  Step 3: 生成最终回复                 │
│                                      │
│  如果 Step 2 返回 0 条：              │
│  Step 2': 调 list_effects() 重新匹配  │ ← 自我纠正
│  Step 2'': 换参数重新 search          │
└──────────────────────────────────────┘
```

#### 可提供的 Tools

| Tool | 数据源 | 作用 | 解决的问题 |
|:-----|:------|:-----|:----------|
| `list_effects` | effect_schema + overlay | 返回所有可用效果名+中文别名 | 效果名映射问题 |
| `list_traits` | effect_schema traits | 返回所有可用特性名 | 特性路由失败问题 |
| `list_classes` | translations.json | 返回所有可用职阶 | 职阶名映射 |
| `search_servants` | servants_db.json (MV) | 按条件搜索从者 | 核心查询 |
| `lookup_servant` | servants_db.json (MV) | 查询单个从者详情 | 从者详情 |
| `compare_servants` | servants_db.json (MV) | 对比多个从者 | 从者对比 |
| `lookup_skill_detail` | Atlas API (runtime) | 查询从者技能的各等级详细数值 | MV 缺失的低频数据（见 5.6） |

#### 对比当前架构

| 维度 | 当前 One-Shot | Agentic Tool Use |
|:-----|:-------------|:----------------|
| **新业务场景** | 每个都要加 Prompt 规则 | 只需新增 tool 定义 |
| **昵称缺失** | 手工加 nicknames.json | Agent 可调 `search_servants(name="仇凛")` 自动模糊匹配 |
| **效果映射** | Prompt 里穷举 55+ 种效果 | Agent 先调 `list_effects()` 查表再搜索 |
| **特性路由** | Prompt 规则引导 | Agent 先调 `list_traits()` 查表再筛选 |
| **试错能力** | 无 | 有（结果为空时可调整参数重试） |
| **Prompt 长度** | 300+ 行，持续增长 | 固定短 system prompt + tools 定义 |
| **LLM 调用次数** | 2 次（路由+生成） | 3-5 次（多轮 tool 调用） |
| **Token 成本** | 低 | 中（多轮增加） |
| **延迟** | 快（2 次 LLM） | 较慢（多轮串行） |

---

## 3. 关键技术问题

### 3.1 LLM API 兼容性 — 验证结果（2026-05-11）

当前使用 **OpenAI Responses API**（`/v1/responses`）。验证结果：

| 提供商 | 模型 | API | tools 支持 | 备注 |
|:------|:-----|:----|:----------|:-----|
| **dashscope** | qwen-plus | Responses API | **✅ 支持** | 正确返回 `function_call`，243 tokens，自动映射"增伤"→`damageBoost` |
| **obao** | claude-sonnet-4-6 | Responses API | **⏳ 待验证** | 本地 DNS 解析失败，需在 ECS 服务器上验证 |

关键发现：
- dashscope Responses API 原生支持 `tools` 参数，返回 `output[].type == "function_call"`
- tool 定义中的 description 足以引导 LLM 做正确的语义映射（"增伤"→`damageBoost`），无需额外 Prompt 规则
- 单次 tool 调用仅 243 tokens，成本可控

待完成：
- [ ] 在 ECS 服务器上验证 obao 提供商的 tools 支持
- [ ] 验证多轮 tool 调用（tool result → 继续调用 → 最终输出）是否正常

### 3.2 延迟控制

Agentic Loop 的多轮 tool 调用会增加延迟。优化策略：
- **本地 tool 即时返回**：`list_effects`/`list_traits` 等查表操作在 Python 内存中完成，< 1ms
- **限制最大轮次**：设置 max_iterations = 5，防止无限循环
- **并行 tool 调用**：部分模型支持一次返回多个 tool call，可并行执行

### 3.3 成本控制

多轮调用增加 Token 消耗。优化策略：
- **精简 tool results**：`list_effects` 只返回 `{name, aliases_zh[0]}`，不返回完整 schema
- **缓存热门查询**：对完全相同的查询返回缓存结果
- **简单查询走 Preset 快捷路径**：已有的 Preset 机制继续生效，跳过 Agent loop

### 3.4 与现有架构的关系

- **SkillExecutor 保持不变**：Agent 的 `search_servants` tool 内部调用 SkillExecutor
- **Skill 模块保持不变**：所有 QuerySkill/ResponseSkill 继续工作
- **Preset 保持不变**：快捷查询不走 Agent loop
- **变更集中在路由层**：只是把"LLM 一步输出 JSON"替换为"LLM 多轮 tool 调用"

---

## 4. 渐进式落地路径

### Phase 1：验证可行性
- 验证 LLM 网关 tools 支持
- 实现最小化 Agent loop（3 个核心 tools）
- 与现有 one-shot 路由 A/B 对比准确率

### Phase 2：全量切换
- 补齐所有 tools
- 优化延迟和 Token 成本
- 迁移所有路由逻辑到 Agent 模式

### Phase 3：业务扩展
- 新增礼装查询 tools
- 新增关卡/素材查询 tools
- 每个新业务域只需新增 tool 定义

---

## 5. 数据链路图

### 5.1 当前架构：One-Shot Prompt 路由

```mermaid
sequenceDiagram
    participant U as 用户
    participant FE as 前端 demo/app.js
    participant API as FastAPI /api/chat/stream
    participant LLM1 as LLM (路由)
    participant SE as SkillExecutor
    participant DB as servants_db.json
    participant LLM2 as LLM (生成)

    U->>FE: "有增伤技能的五星从者"
    FE->>API: POST {message}
    
    Note over API: 构建 300 行路由 Prompt<br/>13 条规则 + 10 个 few-shot<br/>55+ 效果映射全注入

    API->>LLM1: system=路由Prompt, user=用户问题<br/>json_mode=True, response_format=RoutingResponse
    
    Note over LLM1: 必须一步到位输出精确 JSON<br/>无纠错机会

    LLM1-->>API: {"skill_calls": [<br/>  {"skill_name": "search_by_skill_effect",<br/>   "params": {"skillEffect": "damageBoost"}},<br/>  {"skill_name": "search_by_rarity",<br/>   "params": {"op": "eq", "value": 5}}<br/>], "response_skill": "respond_servant_list"}

    API->>SE: execute(skill_calls)
    SE->>DB: filter(damageBoost) AND filter(rarity=5)
    DB-->>SE: 42 条结果
    SE-->>API: ExecutionResult(servants=42)

    Note over API: _build_context(): 预消化 top5<br/>_describe_filters(): 中文描述

    API->>LLM2: system=生成Prompt, user=context_json
    LLM2-->>API: "共找到 42 位五星从者拥有增伤技能..."
    API-->>FE: SSE events (thinking → results → reply)
    FE-->>U: 渲染卡片 + 回复
```

**失败链路（效果映射错误）：**

```mermaid
sequenceDiagram
    participant U as 用户
    participant API as FastAPI
    participant LLM1 as LLM (路由)
    participant SE as SkillExecutor

    U->>API: "能挡伤的从者"
    
    Note over API: 路由 Prompt 里没有<br/>"挡伤"→damageShield 的映射

    API->>LLM1: 路由请求
    LLM1-->>API: {"skill_calls": [<br/>  {"skill_name": "search_by_effect",<br/>   "params": {"effect": "defenceUp"}}<br/>]}
    
    Note over LLM1: LLM 猜了 defenceUp<br/>实际应该是 damageShield（复合效果）

    API->>SE: execute([defenceUp])
    SE-->>API: 只返回 "防御提升" 从者<br/>遗漏了 无敌/回避/护盾 等

    Note over API: 结果不完整，但无法自我纠正<br/>必须人工加 Prompt 规则修复
```

---

### 5.2 新架构：Agentic Tool Use 路由

```mermaid
sequenceDiagram
    participant U as 用户
    participant FE as 前端 demo/app.js
    participant API as FastAPI /api/chat/stream
    participant AG as Agent Loop
    participant LLM as LLM (dashscope/qwen-plus)
    participant TH as Tool Handlers (本地)
    participant SE as SkillExecutor
    participant DB as servants_db.json

    U->>FE: "有增伤技能的五星从者"
    FE->>API: POST {message}
    
    Note over API: 检查 Preset → 未命中<br/>进入 Agent 路由模式

    API->>AG: agent_route(user_message, tools, trace_id)
    
    Note over AG: system prompt 极简 (~30行)<br/>tools 定义从 SKILL_REGISTRY 自动生成

    AG->>LLM: Round 1: instructions + input + tools[]
    LLM-->>AG: function_call: search_servants(<br/>  effects=["damageBoost"],<br/>  source="skill", rarity=5)
    
    Note over AG: 解析 function_call<br/>调用本地 Tool Handler

    AG->>TH: search_servants(effects, source, rarity)
    TH->>SE: execute([search_by_skill_effect, search_by_rarity])
    SE->>DB: filter
    DB-->>SE: 42 条
    SE-->>TH: ExecutionResult
    TH-->>AG: {"total": 42, "top5": [...]}

    AG->>LLM: Round 2: tool result → LLM 生成最终回复
    LLM-->>AG: "共找到 42 位五星从者拥有增伤技能..."
    AG-->>API: AgentResult(reply, servants, tool_trace)

    API-->>FE: SSE events
    FE-->>U: 渲染卡片 + 回复
```

**自我纠正链路（效果映射不确定时）：**

```mermaid
sequenceDiagram
    participant U as 用户
    participant AG as Agent Loop
    participant LLM as LLM
    participant TH as Tool Handlers

    U->>AG: "能挡伤的从者"

    AG->>LLM: Round 1
    
    Note over LLM: 不确定 "挡伤" 对应什么效果<br/>先查表确认

    LLM-->>AG: function_call: list_effects()
    AG->>TH: list_effects()
    TH-->>AG: [<br/>  {"name":"damageShield", "zh":"挡伤害",<br/>   "includes":["invincible","avoidance","gutsHp",...]},<br/>  {"name":"defenceUp", "zh":"防御提升"},<br/>  ...]

    AG->>LLM: Round 2: tool result
    
    Note over LLM: 看到 damageShield 别名包含 "挡伤害"<br/>且是复合效果，自动选择

    LLM-->>AG: function_call: search_servants(<br/>  effects=["damageShield"])
    AG->>TH: search_servants(...)
    TH-->>AG: {"total": 186, "top5": [...]}

    AG->>LLM: Round 3: tool result → 生成回复
    LLM-->>AG: "共找到 186 位从者拥有挡伤能力..."
    
    Note over AG: 3 轮完成，比 one-shot 多 1 轮<br/>但结果正确且完整
```

---

### 5.3 模块交互图：新旧对比

**当前架构（模块依赖）：**

```mermaid
graph TD
    subgraph "路由层 (One-Shot)"
        P[prompts.py<br/>300行路由Prompt<br/>13规则+10示例] --> LC[llm_client.py<br/>chat_completion<br/>json_mode=True]
        S[schemas.py<br/>RoutingResponse<br/>SkillCall] --> LC
    end

    subgraph "执行层 (不变)"
        EX[executor.py<br/>SkillExecutor] --> SK[skills/query/*<br/>11个QuerySkill]
        SK --> DB[(servants_db.json)]
    end

    subgraph "生成层 (不变)"
        GEN[prompts.py<br/>get_generation_prompt] --> LC2[llm_client.py<br/>chat_completion<br/>json_mode=False]
    end

    LC -->|RoutingResponse JSON| M[main.py<br/>_handle_skill_mode]
    M -->|skill_calls| EX
    EX -->|ExecutionResult| M
    M -->|context_json| GEN
    LC2 -->|reply text| M

    style P fill:#ff6b6b,color:#fff
    style S fill:#ff6b6b,color:#fff
```

**新架构（模块依赖）：**

```mermaid
graph TD
    subgraph "路由层 (Agentic)"
        AP[agent_prompt.py<br/>~30行系统Prompt] --> AL[agent_loop.py<br/>AgentLoop<br/>多轮tool调用]
        TD[tool_defs.py<br/>从SKILL_REGISTRY<br/>自动生成tools] --> AL
        AL --> LC[llm_client.py<br/>新增 agent_completion<br/>支持tools参数]
    end

    subgraph "Tool层 (新增)"
        TH[tool_handlers.py<br/>search_servants<br/>list_effects<br/>list_traits<br/>lookup_servant<br/>compare_servants]
    end

    subgraph "执行层 (不变)"
        EX[executor.py<br/>SkillExecutor] --> SK[skills/query/*<br/>11个QuerySkill]
        SK --> DB[(servants_db.json)]
    end

    AL -->|function_call| TH
    TH -->|tool result| AL
    TH -->|内部调用| EX

    AL -->|AgentResult| M[main.py<br/>_handle_agent_mode]
    M -->|reply + servants| FE[前端]

    style AP fill:#51cf66,color:#fff
    style TD fill:#51cf66,color:#fff
    style AL fill:#51cf66,color:#fff
    style TH fill:#51cf66,color:#fff
```

---

### 5.4 Responses API 多轮 Tool 调用协议

```mermaid
sequenceDiagram
    participant APP as agent_loop.py
    participant API as dashscope /v1/responses

    Note over APP,API: Round 1: 用户问题 + tools 定义
    APP->>API: POST /responses<br/>{instructions, input, tools[],<br/> max_output_tokens, temperature}
    API-->>APP: {output: [{type:"function_call",<br/> name:"list_effects", arguments:"{}",<br/> call_id:"call_abc123"}]}

    Note over APP: 本地执行 list_effects()<br/>返回效果列表

    Note over APP,API: Round 2: 上一轮 output + tool result
    APP->>API: POST /responses<br/>{instructions,<br/> input: [<br/>   原始用户消息,<br/>   上一轮 function_call output,<br/>   {type:"function_call_output",<br/>    call_id:"call_abc123",<br/>    output: "[{name:damageBoost,...}]"}<br/> ],<br/> tools[]}
    API-->>APP: {output: [{type:"function_call",<br/> name:"search_servants",<br/> arguments:"{\"effects\":[\"damageBoost\"]}",<br/> call_id:"call_def456"}]}

    Note over APP: 本地执行 search_servants()<br/>→ SkillExecutor → 42条结果

    Note over APP,API: Round 3: tool result → 最终回复
    APP->>API: POST /responses<br/>{instructions,<br/> input: [...之前所有轮次,<br/>   function_call_output for call_def456<br/> ],<br/> tools[]}
    API-->>APP: {output: [{type:"message",<br/> content:[{type:"output_text",<br/> text:"找到42位从者..."}]}]}

    Note over APP: type=="message" → Agent Loop 结束<br/>返回 AgentResult
```

---

### 5.5 整体请求生命周期对比

```mermaid
graph LR
    subgraph "当前架构 (~3s)"
        A1[用户输入] --> A2[Preset检查<br/>~0ms]
        A2 -->|miss| A3[LLM路由<br/>~1.5s<br/>300行Prompt]
        A3 --> A4[SkillExecutor<br/>~50ms]
        A4 --> A5[LLM生成<br/>~1.5s]
        A5 --> A6[SSE响应]
    end

    subgraph "新架构-简单查询 (~3.5s)"
        B1[用户输入] --> B2[Preset检查<br/>~0ms]
        B2 -->|miss| B3[Agent Round1<br/>~1.5s<br/>30行Prompt]
        B3 -->|function_call| B4[Tool执行<br/>~50ms]
        B4 --> B5[Agent Round2<br/>~1.5s<br/>含tool result]
        B5 -->|message| B6[SSE响应]
    end

    subgraph "新架构-复杂查询 (~5s)"
        C1[用户输入] --> C2[Agent Round1<br/>~1.5s]
        C2 -->|list_effects| C3[查表<br/>~1ms]
        C3 --> C4[Agent Round2<br/>~1.5s]
        C4 -->|search_servants| C5[SkillExecutor<br/>~50ms]
        C5 --> C6[Agent Round3<br/>~1.5s]
        C6 -->|message| C7[SSE响应]
    end
```

## 6. 待讨论问题

1. ~~**延迟 vs 准确率 trade-off**~~：用户已确认可接受，准确率和可扩展性优先
2. ~~**Token 成本增加**~~：用户已确认方向可以先做，后续做成本优化
3. ~~**LLM 网关兼容性**~~：dashscope 已验证支持，obao 暂不考虑
4. **回退策略**：Agent loop 失败时是否 fallback 到当前 one-shot 模式？
5. **生成阶段合并**：Agent 最后一轮直接输出用户回复，还是仍然走独立的 RAG 生成 LLM 调用？
