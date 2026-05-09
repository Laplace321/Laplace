# 🌌 Laplace 项目介绍

> **AI Native 对话式 FGO 数据助手** —— 用自然语言查询 Fate/Grand Order 游戏数据

---

## 一、项目进展

### 已完成功能（Phase 1 ~ Phase 6）

| 阶段 | 名称 | 核心成果 |
|:-----|:-----|:---------|
| **Phase 1** | NP 充能查询 | 对话式 MVP 上线：FastAPI + LLM 意图解析 + 从者卡片渲染 |
| **Phase 2** | Schema Mirror 知识库 | `sync_chaldea.py` 从 Chaldea Dart 源码提取 FuncType/BuffType/SkillEffect 领域知识，自动生成知识库 JSON |
| **Phase 3** | 多维度深度查询 | 多条件组合查询、特性（Traits）过滤、从者别名系统（支持"呆毛"、"村正"等社区昵称） |
| **Phase 4** | Two-Step RAG 架构 | 分离 LLM 文案生成与前端结构化渲染，精简上下文防止 Token 爆炸与幻觉 |
| **Phase 5** | 架构治理与可靠性硬化 | LLM Contract（Pydantic 强校验）、Filter Registry、配置热更新、全链路日志追踪、异步日志、GitHub Actions CI |
| **Phase 6** | 查询语义修正 + Skill 架构 | 宝具效果查询、NP 充能三分类（自充/他充/群充）、**Skill-Based Architecture** 迁移、Preset 快捷查询 |

### 计划中功能（Phase 7 ~ Phase 8）

| 阶段 | 名称 | 核心目标 |
|:-----|:-----|:---------|
| **Phase 6 P1** | 数据域扩展 | 特攻对象语义化、伤害计算 ABCD 乘区体系、多语言 UI、概念礼装/魔术礼装数据 |
| **Phase 7** | RAG 架构深化 | 多轮对话上下文（追问式交互）、检索结果智能排序、效果语义召回、空结果 Fallback 策略 |
| **Phase 8** | 原生客户端 | SwiftUI + SwiftData iOS 客户端、语音输入、离线数据缓存 |

---

## 二、产品功能

### 核心能力一览

Laplace 的核心价值是将传统的 **"菜单筛选式"** 游戏工具，变革为 **"自然语言对话式"** 智能助手。

#### 1. 自然语言查询
不再需要在层层菜单中勾选条件，直接用自然语言提问：

| 你说的话 | Laplace 做的事 |
|:---------|:--------------|
| "帮我找一下 30 自充的从者" | 筛选 NP 自充 ≥ 30% 的全部从者 |
| "五星 Caster 有自充的从者" | 组合筛选：稀有度=5 + 职阶=Caster + 有 NP 充能 |
| "有无敌技能的从者" | 按技能效果"无敌"筛选 |
| "能给全队加攻的 Saber" | 效果=攻击力提升 + 目标=全队 + 职阶=Saber |
| "有回避且带毅力的从者" | 多效果 AND 组合查询 |

#### 2. 从者深度档案分析
不仅能查数据，还能让 AI 总结从者的优缺点：
- *"分析下千子村正的技能和宝具"* → AI 自动梳理生存能力、输出增幅、核心卖点
- *"小教授这个从者怎么样？"* → 支持社区昵称，返回通俗易懂的评价

#### 3. 从者对比分析
- *"对比村正和武尊"* → 以表格/分点形式对比优劣势、适用场景

#### 4. 辅助从者推荐
- *"有充能技能的辅助从者"* → 按 NP 充能支持、增伤 Buff、防御生存等维度推荐

#### 5. 覆盖的查询维度

| 维度 | 说明 |
|:-----|:-----|
| **NP 充能** | 自充 / 他充 / 群充，支持数值比较（≥30%、=50% 等） |
| **职阶** | 全 15 个可玩职阶（Saber ~ Pretender） |
| **稀有度** | 1★ ~ 5★ |
| **技能效果** | 55 种细分效果（无敌、毅力、回避、加攻、特攻、即死……） |
| **宝具效果** | 宝具附带效果（降防、特攻、无敌贯通等） |
| **目标类型** | 自身 / 己方全体 / 敌方单体 / 敌方全体 |
| **特性（Traits）** | 秩序·善、龙特性、人科、神性等深度筛选 |
| **配卡** | QAABB / AAABB 等指令卡配置 |
| **宝具色卡** | Arts / Buster / Quick 宝具类型 |
| **属性** | 天 / 地 / 人 / 星 / 兽 |
| **别名** | "呆毛"→Artoria、"C呆"→Altria Caster、"村正"→Muramasa |

#### 6. Preset 快捷入口
前端提供四个一键快捷按钮，无需打字即可触发常见查询：

| Preset | 用途 | 默认行为 |
|:-------|:-----|:---------|
| 🔄 周回筛选 | 筛选适合周回的从者 | NP 充能 ≥ 30% |
| 🔍 从者查询 | 查询单个从者详情 | 需输入从者名 |
| ⚖️ 从者对比 | 对比多个从者 | 需输入从者名 |
| 🛡️ 辅助推荐 | 推荐辅助向从者 | 有充能效果的从者 |

---

## 三、系统架构

### 3.1 整体架构概览

Laplace 采用 **Skill-Based Architecture + Two-Step RAG** 架构，核心流程如下：

```
用户自然语言输入
       │
       ▼
┌──────────────────────┐
│  Stage 1: LLM 路由    │  将自然语言拆解为 Skill 调用组合
│  (Skill Router)       │  输出: RoutingResponse JSON
└──────────┬───────────┘
           │  SkillCall[]
           ▼
┌──────────────────────┐
│  Stage 2: 执行引擎    │  SkillExecutor 按 domain 分组
│  (Skill Executor)     │  AND 合并执行，一次数据扫描
└──────────┬───────────┘
           │  从者结果集
           ▼
┌──────────────────────┐
│  Stage 3: RAG 生成    │  预消化上下文 → ResponseSkill
│  (Response Gen)       │  构建 Prompt → LLM 生成自然语言
└──────────┬───────────┘
           │
           ▼
     SSE 流式返回
  (Thinking Steps + 卡片 + 文字)
```

### 3.2 Skill-Based Architecture（技能模块化架构）

这是 Laplace 最核心的架构设计。所有查询逻辑以 **可独立注册、自由组合** 的 Skill 模块实现，每个查询维度对应一个独立文件。

#### 架构分层

```
server/skills/
├── base.py              ← 基类定义 + SKILL_REGISTRY 全局注册表
├── executor.py           ← SkillExecutor: AND 合并执行引擎
├── presets.py            ← Preset 预设组合（快捷查询入口）
├── query/                ← 10 个 QuerySkill（数据检索）
│   ├── search_by_np_charge.py      NP 充能筛选
│   ├── search_by_class.py          职阶筛选
│   ├── search_by_rarity.py         稀有度筛选
│   ├── search_by_skill_effect.py   技能效果筛选
│   ├── search_by_np_effect.py      宝具效果筛选
│   ├── search_by_traits.py         特性筛选
│   ├── search_by_cards.py          配卡筛选
│   ├── search_by_attribute.py      属性筛选
│   ├── lookup_servant.py           单从者精确查询
│   └── compare_servants.py         多从者对比
└── response/             ← 4 个 ResponseSkill（RAG 生成策略）
    ├── respond_servant_list.py      从者列表回复
    ├── respond_servant_detail.py    单从者详情回复
    ├── respond_servant_compare.py   从者对比回复
    └── respond_support_analysis.py  辅助推荐回复
```

#### 注册机制

每个 Skill 通过 `@register_skill` 装饰器自动注册到 `SKILL_REGISTRY`：

```python
@register_skill
class SearchByNpCharge(QuerySkill):
    name = "search_by_np_charge"
    description = "按 NP 充能量筛选从者（如自充 ≥ 50%）"

    def filter(self, servant: dict, params: dict) -> bool:
        # 每个 Skill 只负责自己的过滤逻辑
        ...
```

**核心优势**：
- **新增查询维度**：只需新增一个 Python 文件 + `@register_skill` 装饰器，无需修改任何已有代码
- **AND 自动合并**：`SkillExecutor` 自动将同 domain 的多个 QuerySkill AND 合并执行，一次数据扫描
- **参数强校验**：每个 Skill 可选提供 Pydantic `params_schema`，校验失败自动跳过并降级

#### 两阶段 LLM 路由

```
用户: "帮我找有无敌技能的五星 Caster"
                │
                ▼
┌─ Stage 1: LLM Routing ────────────────────────┐
│  输入: 用户文本 + 可用 Skill 描述列表            │
│  输出: RoutingResponse                         │
│  {                                             │
│    "skill_calls": [                            │
│      {"skill_name": "search_by_skill_effect",  │
│       "params": {"effect": "invincible"}},     │
│      {"skill_name": "search_by_rarity",        │
│       "params": {"op": "eq", "value": 5}},     │
│      {"skill_name": "search_by_class",         │
│       "params": {"className": "Caster"}}       │
│    ],                                          │
│    "response_skill": "respond_servant_list"    │
│  }                                             │
└────────────────────────────────────────────────┘
                │
                ▼
        SkillExecutor.execute()
        → 三个 Skill AND 合并 → 结果集
```

`RoutingResponse` 由 **Pydantic 模型** 强约束，通过 OpenAI `response_format/json_schema` 模式调用 LLM，确保结构化输出的可靠性。

### 3.3 Two-Step RAG 架构

Laplace 的 RAG（检索增强生成）分为 **检索** 和 **生成** 两个独立阶段：

```
┌─────────────────────────────────────────────────────┐
│                   Step 1: Retrieve                   │
│                                                      │
│  LLM 意图解析 → SkillExecutor 执行                    │
│  → 从 servants_db.json 检索匹配从者                    │
│  → 按稀有度降序排序                                    │
└──────────────────────┬──────────────────────────────┘
                       │ 原始结果集
                       ▼
┌─────────────────────────────────────────────────────┐
│              预消化 (Pre-digestion)                   │
│                                                      │
│  英文枚举 → 中文:                                     │
│  • className: "caster" → "术阶"                      │
│  • npCard: "arts" → "蓝卡"                           │
│  • skillEffects: "invincible" → "无敌"               │
│                                                      │
│  精简上下文:                                          │
│  • 取 Top 5 条详细数据 + total_found 总数              │
│  • 注入 __internal 内部判定字段（色卡增强状态）          │
└──────────────────────┬──────────────────────────────┘
                       │ 中文化 Context JSON
                       ▼
┌─────────────────────────────────────────────────────┐
│                   Step 2: Generate                   │
│                                                      │
│  ResponseSkill.build_prompt() 构建专用 Prompt          │
│  → LLM 基于预消化数据生成自然语言回复                   │
│  → 严禁使用先验知识，只能基于注入数据回答               │
└─────────────────────────────────────────────────────┘
```

**关键设计决策**：
- **LLM 不直接查数据**：LLM 只负责意图解析和结果格式化，不接触原始数据库
- **预消化消除幻觉**：所有英文枚举在投喂 LLM 前已转为中文，杜绝翻译幻觉（如"必中"被误译为"破盾"）
- **Top-N 精简策略**：最多取 5 条详细数据 + 总数统计，防止 Token 爆炸
- **多种 ResponseSkill**：列表回复、详情回复、对比分析、辅助推荐各有专用 Prompt 策略

### 3.4 Schema Mirror 知识库

Laplace 不复制 Chaldea 的 Dart 代码，而是 **提取其领域建模知识** 注入到 LLM 和数据处理管线中。

```
Chaldea Dart 源码（5 个核心文件）
       │  python3 server/sync_chaldea.py
       ▼
┌──────────────────────────────────┐
│  server/knowledge/                │
│  ├── effect_schema.json           │  55+ 种效果分类 → FuncType/BuffType 映射
│  ├── class_mapping.json           │  15 个可玩职阶中日英映射
│  ├── func_types.json              │  165+ FuncType 枚举完整列表
│  ├── buff_types.json              │  200+ BuffType 枚举完整列表
│  └── _meta.json                   │  Chaldea git commit hash + 提取时间戳
└──────────────────────────────────┘
       │
       ▼
  LLM Prompt 注入 + 查询过滤 + 翻译映射
```

**同步策略**：
- **纯正则解析**：不依赖 Dart SDK，Python 正则直接从 `.dart` 源文件提取枚举和映射
- **幂等操作**：重复运行不产生副作用，每次覆盖旧文件
- **版本追踪**：`_meta.json` 记录 Chaldea commit hash + 时间戳
- **非 Runtime 依赖**：Chaldea 源码仅在运行 `sync_chaldea.py` 时需要，普通部署只依赖已生成的 JSON

### 3.5 数据模型

#### 从者数据模型 (`servants_db.json`)

每个从者记录包含以下核心字段：

```json
{
  "id": 100100,
  "collectionNo": 1,
  "name": "Altria Pendragon",
  "originalName": "アルトリア・ペンドラゴン",
  "aliasCN": "阿尔托莉雅·潘德拉贡",
  "className": "saber",
  "rarity": 5,
  "attribute": "earth",
  "cards": ["quick", "arts", "arts", "buster", "buster"],
  "npCard": "buster",
  "npTarget": "all",
  "traits": ["dragon", "saberface", "king", "riding", "arthurOrAltria"],
  "gender": "female",
  "hasNpCharge": true,
  "totalCharge": 30,
  "npCharges": [
    {"chargePercent": 30, "targetType": "self", "source": "skill", "skillId": 5400}
  ],
  "maxPtOneCharge": 0,
  "maxPtAllCharge": 0,
  "skillEffects": ["upBuster", "upAtk", "invincible", "gainNp", ...],
  "npEffects": ["upNpdamage"],
  "skillDetails": [
    {
      "skillId": 5400,
      "name": "Charisma B",
      "effects": [
        {"type": "upAtk", "targetType": "party", "value": 18}
      ]
    }
  ]
}
```

#### LLM 路由契约 (`RoutingResponse`)

```python
class RoutingResponse(BaseModel):
    skill_calls: list[SkillCall]    # 要执行的 Skill 调用列表
    response_skill: str             # 回复策略 Skill 名称
    fallback: FallbackReason | None # 降级原因（无法匹配时）

class SkillCall(BaseModel):
    skill_name: str                 # Skill 名称（如 "search_by_class"）
    params: dict                    # Skill 参数
```

#### 效果知识库 (`effect_schema.json`)

```json
{
  "effects": [
    {
      "name": "invincible",
      "aliases_zh": ["无敌"],
      "funcTypes": [],
      "buffTypes": ["invincible"]
    },
    {
      "name": "gainNp",
      "aliases_zh": ["NP充能", "充能"],
      "funcTypes": ["gainNp"],
      "buffTypes": []
    }
  ]
}
```

### 3.6 技术栈总览

| 层级 | 技术 | 说明 |
|:-----|:-----|:-----|
| **前端** | HTML / Vanilla CSS / Vanilla JS | 极简单页应用，SSE 流式渲染 |
| **后端** | Python 3.12+ / FastAPI / Uvicorn | 异步 Web 框架 |
| **LLM 通信** | OpenAI Responses API / httpx | 多提供商降级（同模型 retry → 跨提供商 fallback） |
| **数据校验** | Pydantic v2 | LLM 输出契约 + Skill 参数校验 |
| **知识提取** | 正则解析 Dart 源码 | `sync_chaldea.py` → JSON 知识库 |
| **数据源** | Atlas Academy API + Chaldea | 从者全量数据 + 领域知识 |
| **日志** | JSONL 结构化日志 | asyncio.to_thread 非阻塞写入 |
| **CI/CD** | GitHub Actions + ruff | 自动 lint + format + test |

---

## 四、交互特点

### 4.1 Thinking Steps 流式体验

Laplace 采用 **SSE（Server-Sent Events）** 实时推送 AI 的思考过程，用户不再面对空白等待画面：

```
发送问题
  │
  ├─ 🧠 "正在理解你的问题..."        ← Phase: routing
  │     LLM 正在解析意图
  │
  ├─ ✅ "意图识别完成"                ← Phase: routed
  │     展示识别出的 Skill 调用组合
  │
  ├─ 🔍 "正在检索从者数据..."        ← Phase: executing
  │     SkillExecutor 执行查询
  │
  ├─ 📦 从者卡片立即弹出              ← Phase: cards
  │     数据查到后卡片先行渲染
  │
  ├─ ✍️ "正在生成分析..."            ← Phase: generating
  │     LLM 基于预消化数据生成文案
  │
  └─ 📝 AI 文字回复流式显示            ← Phase: done
        Markdown 富文本渲染
```

**零额外 Token 消耗**：Thinking Steps 完全由后端状态机驱动，不消耗 LLM Token。

### 4.2 多入口交互模式

| 模式 | 触发方式 | LLM 调用 | 适用场景 |
|:-----|:---------|:---------|:---------|
| **自由对话** | 输入框直接打字 | Stage 1 路由 + RAG 生成 | 任意自然语言查询 |
| **Preset 快捷** | 点击预设按钮 | 跳过路由，可选 RAG 生成 | 高频常见查询 |
| **Preset + 补充** | 点击预设 + 输入补充条件 | 补充文字走路由解析 | 在预设基础上细化条件 |

### 4.3 从者卡片可视化

查询结果以精美卡片形式呈现，关键数值一目了然：
- **星级标识**：稀有度直观显示
- **职阶图标**：15 个职阶对应图标
- **NP 充能**：显示自充/他充/群充百分比及类型标签
- **宝具色卡**：Arts（蓝）/ Buster（红）/ Quick（绿）颜色标识
- **配卡组成**：QAABB 等指令卡可视化

### 4.4 智能降级与容错

| 场景 | 处理策略 |
|:-----|:---------|
| LLM 路由失败 | 返回友好提示，建议更具体的描述 |
| Skill 参数校验失败 | 跳过该 Skill，其余继续执行 |
| 查询结果为空 | 返回"未找到匹配从者"并建议调整条件 |
| LLM API 超时 | 3 次 retry + 指数退避（1s/2s/4s）→ 跨提供商降级 |
| RAG 生成失败 | 降级为纯数字统计回复（"为你找到了 N 位从者"） |

### 4.5 开发者调试体验

- **Trace Debug Panel**：按 `Ctrl+D` 唤出隐藏调试面板，查看当前对话 trace_id
- **Trace API**：`/api/traces` 和 `/api/traces/{trace_id}` 只读接口（仅 localhost）
- **结构化日志**：JSONL 格式记录每一步的输入输出，通过 trace_id 全链路回溯

---

## 五、数据流全景

```
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
│  Chaldea     │    │ Atlas Academy│    │  Community       │
│  Dart 源码   │    │    API       │    │  Nicknames       │
└──────┬──────┘    └──────┬───────┘    └────────┬────────┘
       │                  │                     │
  sync_chaldea.py    data_loader.py        config/nicknames.json
       │                  │                     │
       ▼                  ▼                     ▼
┌─────────────┐    ┌──────────────┐    ┌─────────────────┐
│  knowledge/  │    │ servants_db  │    │  config/         │
│  *.json      │    │   .json      │    │  *.json          │
└──────┬──────┘    └──────┬───────┘    └────────┬────────┘
       │                  │                     │
       └──────────┬───────┘─────────────────────┘
                  │
                  ▼
         ┌────────────────┐
         │  FastAPI Server │
         │  (main.py)      │
         └───────┬────────┘
                 │ SSE / JSON
                 ▼
         ┌────────────────┐
         │  Web Frontend   │
         │  (demo/)        │
         └────────────────┘
```

---

> **Laplace**：以数学家拉普拉斯之名，观测月球数据的真理之眼，现已就绪。
