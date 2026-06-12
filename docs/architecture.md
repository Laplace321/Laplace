# Laplace 项目架构文档

> 本文档完全基于源代码分析生成，最后更新：2026-06-12

## 1. 项目概览

Laplace 是一个 FGO（Fate/Grand Order）游戏助手，以自然语言问答为核心交互方式。用户输入自然语言查询，系统通过 LLM 路由到合适的技能模块，执行结构化数据库查询或攻略检索，最终生成面向玩家的中文回复。

**技术栈**：Python 3.12 / FastAPI / SSE / BM25 / 多 LLM 供应商（OpenAI / Obao / Dashscope）

**核心设计理念**：
- 两阶段 LLM 路由（分类器 → 技能路由器），而非端到端生成
- 结构化数据查询优先，LLM 仅负责理解意图和生成回复
- 所有英文枚举在构建时预翻译为中文，运行时零翻译开销
- 多供应商 LLM 容灾，两层降级（同供应商备选模型 → 跨供应商降级）

---

## 2. 系统架构总览

```
用户输入
  │
  ▼
┌──────────────┐
│  FastAPI 入口  │  main.py — JSON(/chat) + SSE(/chat/stream)
└──────┬───────┘
       │
       ▼
┌──────────────┐     ┌──────────┐
│ Stage 0 分类器 │────▶│ LLM 调用  │  prompts.build_classifier_prompt()
└──────┬───────┘     └──────────┘
       │
       ├─── Pipeline A（结构化查询）
       │       │
       │       ▼
       │    ┌──────────────┐     ┌──────────┐
       │    │ Stage 1 路由器 │────▶│ LLM 调用  │  prompts.build_routing_prompt()
       │    └──────┬───────┘     └──────────┘
       │           │
       │           ▼
       │    ┌──────────────┐
       │    │  技能执行器     │  skills/executor.py — 查询技能链
       │    └──────┬───────┘
       │           │
       │           ▼
       │    ┌──────────────┐     ┌──────────┐
       │    │  回复技能       │────▶│ LLM 调用  │  响应生成
       │    └──────────────┘     └──────────┘
       │
       ├─── Pipeline B（Atlas 知识问答）
       │       │
       │       ▼
       │    ┌──────────────┐     ┌──────────┐
       │    │  Agent 循环    │────▶│ LLM 调用  │  agent/agent_loop.py
       │    └──────┬───────┘     └──────────┘
       │           │
       │           ▼
       │    ┌──────────────┐
       │    │  7 个工具调用   │  tool_handlers.py
       │    └──────────────┘
       │
       └─── Pipeline C（攻略检索）
               │
               ▼
            ┌──────────────┐
            │ BM25 文档检索  │  guide_retriever.py
            └──────┬───────┘
                   │
                   ▼
            ┌──────────────┐     ┌──────────┐
            │  攻略回复生成   │────▶│ LLM 调用  │
            └──────────────┘     └──────────┘
```

---

## 3. 入口层（`server/main.py`，498 行）

### 3.1 FastAPI 应用配置

- **中间件**：CORS（允许所有来源）+ 自定义速率限制
- **启动流程**（`@app.on_event("startup")`）：
  1. `load_database()` — 加载从者/CE/效果数据库
  2. `validate_translations()` — 校验翻译完整性
  3. `validate_skill_registry()` — 校验技能注册表一致性
  4. `start_probe_loop()` — 启动 LLM 模型健康探测

### 3.2 API 路由

| 路由 | 方法 | 说明 |
|:---|:---|:---|
| `/chat` | POST | JSON 模式请求，返回完整响应 |
| `/chat/stream` | POST | SSE 流式请求，逐步返回事件 |
| `/presets` | GET | 获取预设列表 |
| `/admin/config/{name}` | GET/PUT | 配置文件读写 |
| `/admin/env` | GET/PUT | 环境变量管理 |
| `/admin/logs` | GET | 日志查询 |
| `/admin/metrics` | GET | Prometheus 指标 |
| `/admin/health` | GET | 健康检查 |
| `/faces/{servant_id}` | GET | 从者头像代理 |

### 3.3 请求模型

```python
class ChatRequest:
    message: str              # 用户输入
    mode: str = "skill"       # 固定为 "skill"
    preset_name: str | None   # 预设名称
    params: dict | None       # 预设参数
    response_skill: str | None  # 指定回复技能
    confirmation_context: dict | None  # 确认上下文（跳过路由）
```

**B1 合并策略**：当同时有 `preset_name` 和 `message` 时，将预设文本与用户文本合并为组合查询。

---

## 4. 三管线路由系统（`server/pipeline.py`，2318 行）

### 4.1 Stage 0：三路分类器

入口函数：`_classify_query()`

调用 `prompts.build_classifier_prompt()` 生成分类 prompt，LLM 返回 JSON：

```json
{
  "pipeline": "A" | "B" | "C",
  "reason": "分类理由",
  "tags": ["coronation", "saber"]  // Pipeline C 专用
}
```

**分类规则**（5 条消歧规则）：
1. 可用结构化数据回答 → Pipeline A
2. 需要 Atlas Academy 机制细节 → Pipeline B
3. 攻略/配队/战术相关 → Pipeline C
4. 混合查询偏向结构化 → Pipeline A
5. 无法分类 → Pipeline B（Agent 兜底）

### 4.2 Pipeline A：结构化查询

**流程**：`_classify_query()` → `_route_to_skills()` → `SkillExecutor.execute()` → 回复技能 → LLM 生成

1. **Stage 1 路由器**（`_route_to_skills()`）：调用 LLM 将自然语言映射为 1-3 个 SkillCall
2. **技能执行**（`SkillExecutor`）：执行查询技能链，返回从者/CE 数据
3. **上下文构建**（`context_builder.py`）：将查询结果格式化为 LLM 可读文本
4. **回复生成**：选定的 ResponseSkill 构建 prompt，LLM 生成最终回复

**确认直通路径**：当 `confirmation_context` 包含 `collectionNo` 时，跳过路由，直接查库。

### 4.3 Pipeline B：Atlas 知识问答

**流程**：`_handle_atlas_pipeline()` → `agent_route()` → 工具调用循环 → 事实核验

- Agent 最多执行 8 轮工具调用
- 返回 `AgentResult`（文本 + 可选的卡片渲染数据）
- **事实核验**（`_verify_atlas_facts()`）：用 LLM 对比 Agent 回复与数据库中的从者数据，阈值 70%

### 4.4 Pipeline C：攻略检索

**流程**：`_handle_guide_pipeline()` → `GuideRetriever.search()` → LLM 生成

- `_extract_guide_tags()` 从分类结果中提取 tags 用于过滤
- 检索命中文档全文传入 LLM context
- 生成 prompt 限制 800 字符（戴冠攻略）

### 4.5 SSE 事件流

`stream_event_generator()` 通过 SSE 推送 6 种事件类型：

| 事件类型 | 说明 |
|:---|:---|
| `thinking` | 路由/分类思考过程 |
| `servants` | 从者/CE 卡片数据（JSON） |
| `clarification` | 澄清请求（多候选/空结果） |
| `delta` | 文本流式增量 |
| `done` | 完成信号 |
| `error` | 错误信息 |

---

## 5. 技能系统（`server/skills/`）

### 5.1 基础架构（`base.py`）

```python
class QuerySkill(BaseSkill):
    domain: str           # "servant" | "ce" | "knowledge"
    params_schema: dict   # 参数 JSON Schema
    def filter(params, db) -> list     # 过滤数据库
    def execute(params, db) -> Result  # 执行查询

class ResponseSkill(BaseSkill):
    def build_prompt(context, query) -> str  # 构建生成 prompt

SKILL_REGISTRY: dict[str, BaseSkill]  # 全局技能注册表
@register_skill(name)                 # 注册装饰器
```

### 5.2 查询技能（19 个）

**从者域（14 个）**：

| 技能 | 功能 | 关键实现细节 |
|:---|:---|:---|
| `lookup_servant` | 按名字查从者 | 三阶段匹配：精确 → 模糊 → 昵称 |
| `compare_servants` | 比较多个从者 | 支持 2-5 个从者对比 |
| `resolve_nickname` | 昵称解析 | 同步缓存 + 异步 LLM 猜测 |
| `search_by_effect` | 按技能效果搜索 | 复合效果、CD 匹配、值转换 |
| `search_by_class` | 按职阶搜索 | — |
| `search_by_rarity` | 按稀有度搜索 | — |
| `search_by_cards` | 按指令卡搜索 | — |
| `search_by_traits` | 按特性搜索 | trait 别名解析、阵营拆分 |
| `search_by_attribute` | 按属性搜索 | — |
| `search_by_class_advantage` | 按克制关系搜索 | — |
| `search_by_skill_effect` | 按主动技能效果搜索 | — |
| `search_by_np_effect` | 按宝具效果搜索 | — |
| `coronation_knowledge` | 戴冠战知识 | — |
| `coronation_team` | 戴冠战配队 | — |

**概念礼装域（5 个）**：

| 技能 | 功能 |
|:---|:---|
| `ce_lookup` | 按名字查 CE |
| `ce_search_by_effect` | 按效果搜索 CE |
| `ce_search_by_rarity` | 按稀有度搜索 CE |
| `ce_search_by_atk_type` | 按攻/HP 类型搜索 CE |
| `ce_search_by_obtain` | 按获取方式搜索 CE |

### 5.3 回复技能（6 个）

| 技能 | 功能 | 特殊逻辑 |
|:---|:---|:---|
| `respond_servant_list` | 从者列表回复 | — |
| `respond_servant_detail` | 从者详情回复 | 含技能/宝具详细数据 |
| `respond_servant_compare` | 从者对比回复 | 多维度对比表 |
| `respond_support_analysis` | 辅助推荐回复 | — |
| `respond_coronation` | 戴冠攻略回复 | 800 字符限制 |
| `respond_ce_list` | CE 列表回复 | — |

### 5.4 技能执行器（`executor.py`）

**执行流程**：
1. 按域（servant/ce/knowledge）分组 SkillCall
2. 同域多技能使用 AND 合并过滤条件
3. 查询数据库获取结果

**四级降级机制**：
1. **主查询失败** → 放宽过滤条件（relaxation suggestions）
2. **名字未匹配** → 昵称解析（`resolve_nickname`）
3. **昵称解析失败** → LLM 候选猜测
4. **仍无结果** → Agent 工具调用循环兜底

**澄清类型**（`ClarificationRequest`）：
- `multi_candidate` — 多个候选从者，需用户选择
- `empty_result_name` — 名字未匹配
- `empty_result_filter` — 条件过滤后无结果

### 5.5 预设系统（`presets.py`）

4 个预设模板，将常见查询模式封装为一键操作：

| 预设 | 映射技能 |
|:---|:---|
| `cycle_farming` | search_by_effect + respond_servant_list |
| `servant_lookup` | lookup_servant + respond_servant_detail |
| `servant_compare` | compare_servants + respond_servant_compare |
| `support_recommend` | search_by_effect + respond_support_analysis |

---

## 6. LLM 适配层（`server/llm/`）

### 6.1 基础抽象（`base.py`）

```python
class BaseLLMAdapter(ABC):
    async def chat_completion(messages, json_mode, json_schema) -> str
    async def agent_completion(messages, tools) -> AgentResponse
    async def chat_completion_stream(messages) -> AsyncIterator[str]
```

**重试机制**：`_retry_loop()`，MAX_RETRIES=3，退避间隔 [1.0, 2.0, 4.0] 秒。

**JSON 提取**：`extract_json_object()` 从 LLM 回复中提取 JSON（处理 markdown 代码块等）。

### 6.2 供应商管理（`provider.py`）

**配置方式**：
- **新格式**：`LLM_PROVIDERS=name1,name2` + 每个供应商独立 `{NAME}_URL`/`{NAME}_KEY`/`{NAME}_MODELS`
- **旧格式**：单一 `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL`

**两层降级**：
1. 同供应商内多模型轮转
2. 跨供应商降级（provider A 全部不可用 → provider B）

**错误分类**（`_classify_llm_error()`）：
- `rate_limit` — 429 限流
- `timeout` — 请求超时
- `auth` — 401/403 认证失败
- `bad_request` — 400 请求格式错误
- `server` — 5xx 服务端错误
- `unknown` — 未知错误

**跨供应商兼容**：`_sanitize_tool_messages()` 处理不同 API 的 tool message 格式差异。

### 6.3 适配器实现

| 适配器 | API 协议 | JSON 降级策略 |
|:---|:---|:---|
| `OpenAIAdapter` | Responses API（chat）/ Chat Completions（agent） | json_schema(strict=True) → text_fallback |
| `ObaoAdapter` | Chat Completions API | json_schema(strict=False) → json_object → text_fallback |
| `DashscopeAdapter` | 同步 SDK + asyncio.to_thread() | json_object → text_fallback |

---

## 7. Agent 系统（`server/agent/`）

### 7.1 Agent 循环（`agent_loop.py`）

```python
async def agent_route(query, db, llm, max_rounds=8) -> AgentResult
```

- 每轮：LLM 决策 → 工具调用 → 结果注入 → 下一轮
- `AgentResult` 包含文本回复 + 可选的卡片渲染数据（`_CARD_TOOLS`）
- `oneshot_context` 注入防止重复工具调用

### 7.2 工具定义（`tool_defs.py`，7 个工具）

| 工具 | 参数数量 | 说明 |
|:---|:---|:---|
| `search_servants` | 15 | 多条件组合搜索从者 |
| `lookup_servant` | 1 | 按名字查找从者详情 |
| `compare_servants` | 1 | 对比多个从者 |
| `list_effects` | 0 | 列出所有可搜索效果 |
| `list_traits` | 0 | 列出所有特性 |
| `list_classes` | 0 | 列出所有职阶 |
| `lookup_skill_detail` | 1 | 调用 Atlas Academy API 查技能详情 |

### 7.3 工具处理器（`tool_handlers.py`）

- `handle_search_servants`：将 15 个参数映射为最多 7 个 SkillCall
- `handle_lookup_skill_detail`：唯一调用外部 API（Atlas Academy）的工具
- `TOOL_HANDLERS` 字典注册所有处理函数

### 7.4 Agent Prompt（`agent_prompt.py`）

- 10 条工具使用准则
- 分类标签：`[GREETING]`/`[OUT_OF_SCOPE]`/`[UNSUPPORTED]`
- fallback.py 解析这些标签并返回模板消息

---

## 8. 数据构建层

### 8.1 主数据加载（`server/data_loader.py`，1757 行）

**数据源**：Atlas Academy API（`api.atlasacademy.io`）

**构建流程**：
```
Atlas API → 原始数据 → 效果匹配 → 翻译注入 → 物化视图 → 序列化 JSON
```

**核心机制**：

1. **声明式效果匹配**：从 Chaldea Dart 源码提取的验证规则，支持 6 种验证类型
   - `check_buff` — Buff 类型匹配
   - `check_func` — 函数类型匹配
   - `check_trait` — 特性 trait 匹配
   - `check_vals` — 数值范围匹配
   - `check_team` — 队伍目标匹配
   - `lambda` — 自定义验证逻辑

2. **三层 Trait 解析**：
   - Atlas 原始 trait → Chaldea 中文翻译 → 自定义别名

3. **物化视图**（构建时计算，运行时直接查询）：
   - `skillEffects` — 主动技能效果列表
   - `npEffects` — 宝具效果列表
   - `totalSelfCharge` — 自充能总量

4. **预消化（Pre-Digestion）**：所有英文枚举值在构建时翻译为中文，运行时零翻译开销

5. **partyOther 重分类**：FGO 特有的 buff 语义优化，根据 `funcTargetType` 将 `partyOther` 效果重分类为对自身或对队友

6. **CE entryFunction 机制**：概念礼装的初始效果提取

### 8.2 Schema 同步（`server/sync_chaldea.py`，752 行）

**数据源**：Chaldea App Dart 源码（GitHub）

**同步内容**：
- `FuncType`（130 个枚举值）
- `BuffType`（246 个枚举值）
- `SvtClass`（78 个枚举值）
- `SkillEffect` 定义与验证规则
- 多语言翻译（日/中/英/韩）

**输出**：`server/knowledge/` 目录下的 JSON 文件

### 8.3 昵称同步（`extractor/sync_mooncell_nicknames.py`，358 行）

**数据源**：Mooncell Wiki（fgo.wiki）MediaWiki API

**流程**：
```
枚举从者分类页面 → 批量获取 wikitext → 正则提取昵称/序号 → collectionNo 映射 → 写入 JSON
```

**输出**：`server/config/mooncell_nicknames.json`

### 8.4 数据文件结构

```
server/
├── data/
│   ├── servants_db.json          # 从者数据库（Atlas 构建）
│   ├── ce_db.json                # 概念礼装数据库
│   ├── atlas_cn_index.json       # Atlas 中文索引
│   └── guides/                   # 攻略文档（Pipeline C）
│       ├── coronation_general.md
│       ├── coronation_saber.md
│       └── coronation_berserker.md
├── config/
│   ├── nicknames.json            # 手工昵称表（优先级最高）
│   ├── mooncell_nicknames.json   # Mooncell 昵称表（自动生成）
│   ├── effect_hints.json         # 效果别名提示
│   └── trait_aliases.json        # 特性别名
└── knowledge/
    ├── func_type.json            # FuncType 枚举
    ├── buff_type.json            # BuffType 枚举
    ├── svt_class.json            # SvtClass 枚举
    ├── skill_effect.json         # SkillEffect 定义
    └── translations/             # 多语言翻译
```

---

## 9. 查询执行与上下文构建

### 9.1 查询执行器（`server/query_executor.py`，366 行）

- `load_database()` — 加载从者/CE/效果数据
- `load_nicknames()` — 两层昵称加载（mooncell 基础层 + 手工覆盖层，通过 `CachedConfig` 热加载）
- `_match_effect()` / `_match_np_effect()` — 效果匹配，含 partyOther 重分类
- `_normalize_text()` — 全角字符转半角 + 大小写归一化

### 9.2 上下文构建器（`server/context_builder.py`，366 行）

- `MAX_CONTEXT_SIZE = 5`（详情模式上限）
- `MAX_RESULTS = 50`（列表模式上限）
- `build_context()` — 构建从者上下文文本，detail 模式包含技能/宝具详细数据
- `build_ce_context()` — 构建 CE 上下文
- `format_effect_detail()` — 效果值的单位转换（如千分比→百分比）

### 9.3 翻译层（`server/translation.py`，231 行）

- `get_class_map()` — 职阶中英映射
- `get_effect_translation()` — 效果翻译
- `effect_qualifier()` — 效果修饰符
- `describe_filters()` — 将过滤条件翻译为中文描述（支持 20+ 技能类型）

---

## 10. 攻略检索引擎（`server/guide_retriever.py`，176 行）

**架构**：方案 D — 文档级 BM25 + 全文传入（参见 `docs/architecture-discussions/guide-retrieval-strategy-evolution.md`）

### 10.1 核心组件

- `GuideChunk` — 文档块，含内容、元数据、分数
- `GuideRetriever` — 检索引擎主类

### 10.2 索引构建

1. 加载 `server/data/guides/*.md`
2. 解析 YAML frontmatter（title/tags/author/updated）
3. 按 `##` 标题切分文档为 chunk
4. 中文 bigram 分词 + BM25Okapi 索引

### 10.3 检索策略

1. **文档级评分**：同一文档所有 chunk 取 max 分数作为文档分数
2. **Tag 过滤**：Stage 0 分类器提取的 tags 用于预筛选
3. **通用文档自动补召**：命中文档的同系列 "通用" 标签文档自动拉入
4. **全文传入**：返回命中文档的所有 chunk（而非 top_k 个 chunk）
5. **向量兜底**：`_vector_search()` 接口已预留，当前返回空（一期）

### 10.4 分词器

```python
# 简易中文分词：连续中文 + 英文单词 + 中文 bigram 扩展
tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())
# 长中文串额外做 bigram 切分以提升召回
```

---

## 11. 前端

### 11.1 Demo 前端（`demo/`）

**技术栈**：纯 HTML/CSS/JS，无框架依赖

#### `demo/app.js`（1385 行）

**SSE 通信**：通过 `ReadableStream` 处理 SSE 事件流，60 秒超时。

**核心功能**：
- **消息渲染**：支持从者卡片（含头像、职阶、稀有度边框）、CE 卡片、文本回复
- **思考动画**：thinking 事件触发带动画图标的思考步骤展示
- **澄清流程**：`sendWithConfirmation()` 处理多候选选择
- **会话管理**：localStorage 存储，最多 20 个会话
- **评价系统**：每条回复可评分
- **调试面板**：Ctrl+D 切换，显示路由/技能调用/Token 使用详情
- **数据分析**：`sendBeacon` 上报用户行为

#### `demo/style.css`（1601 行）

- **主题**：深色主题（`#0a0b1a` 背景），金/紫/青强调色
- **字体**：Noto Sans SC + Orbitron
- **响应式**：4 个断点（768/480/360px）
- **卡片样式**：稀有度决定边框颜色（5星金、4星紫、3星蓝、2星绿、1星灰）
- **动画**：骨架屏加载、脉冲、渐变等

#### `demo/changelog.js`（前端更新日志模块）

#### `demo/logs.js`（前端日志查看模块）

### 11.2 Admin 后台（`admin/`）

**技术栈**：纯 HTML/CSS/JS

#### `admin/admin.js`（427 行）

**三个 Tab 页**：

1. **环境变量管理**：支持文件模式（`.env`）和环境注入模式，在线编辑环境变量
2. **配置文件管理**：JSON 配置文件的在线编辑，含 JSON 语法校验
3. **监控面板**：LLM 指标（请求量/延迟/成功率）、模型可用性状态、30 秒自动刷新

#### `admin/auth.py`（Admin 认证模块）

基于 API Key 的简单认证，Key 来自 `ADMIN_API_KEY` 环境变量。

#### `admin/routes.py`（Admin 路由注册）

注册所有 `/admin/*` 路由到 FastAPI 应用。

---

## 12. 监控与基础设施

### 12.1 日志系统（`server/logger.py`，528 行）

- **格式**：JSONL 结构化日志
- **15 种 Phase 类型**：覆盖完整请求生命周期
- `find_trace(trace_id)` — 按 trace ID 聚合日志
- `compute_log_stats()` — UV 统计（基于 IP hash）

### 12.2 速率限制（`server/rate_limiter.py`，132 行）

- **双层限制**：per-IP + 全局
- **滑动窗口**：60 秒清理周期
- **IP 提取**：支持 `X-Forwarded-For` 头

### 12.3 指标收集（`server/monitor/metrics.py`，399 行）

- `MetricsCollector` — 60 分钟环形缓冲区
- **被动告警**：连续失败触发
- **输出格式**：Prometheus text exposition

### 12.4 告警系统（`server/monitor/alerter.py`，209 行）

- **双通道**：Bark（iOS 推送）+ Telegram
- **去重**：30 分钟去重窗口
- **历史**：100 条告警记录

### 12.5 健康检查（`server/monitor/health_checker.py`，138 行）

- **主动探测**：定期向 LLM 供应商发送 probe 请求
- **可配间隔**：默认间隔可通过环境变量配置
- **阈值**：连续 2 次失败标记为不可用

---

## 13. Prompt 工程（`server/prompts.py`，521 行）

### 13.1 分类器 Prompt（`build_classifier_prompt()`）

- 三路分类（A/B/C）+ 5 条消歧规则
- 输出 JSON：pipeline + reason + tags

### 13.2 路由器 Prompt（`build_routing_prompt()`）

- 18+ 条路由规则
- 效果提示注入（`effect_hints.json`）
- 输出 JSON：1-3 个 SkillCall + response_skill
- 约 450 行，是最复杂的 prompt

### 13.3 生成 Prompt（`get_generation_prompt()`）

- 9 条生成原则
- 6 项检查清单
- 控制回复质量和格式

---

## 14. 部署架构

### 14.1 Docker 构建（`Dockerfile`）

```
Python 3.12-slim 基础镜像
  → pip install requirements.txt
  → 复制 server/ + demo/ + admin/
  → 入口: docker-entrypoint.sh
```

### 14.2 启动流程（`docker-entrypoint.sh`）

```bash
1. python server/data_loader.py    # 构建数据库（从 Atlas API 拉取）
2. python server/sync_chaldea.py   # 同步 Chaldea Schema
3. nginx                           # 启动 Nginx（前端静态 + 反代）
4. uvicorn server.main:app         # 启动 FastAPI
```

### 14.3 Nginx 配置（`nginx.conf`）

```
/           → demo/          # 前端
/admin      → admin/         # 管理后台
/api/*      → uvicorn:8000   # API 反代
```

### 14.4 CI/CD（`.github/workflows/ci.yml`）

- 触发：push to main / PR to main
- 步骤：lint（ruff）→ test（pytest）→ Docker build → push to registry

---

## 15. 测试

**测试文件**位于 `tests/` 目录：

| 文件 | 覆盖范围 |
|:---|:---|
| `test_context_builder.py` | 上下文构建 |
| `test_data_loader.py` | 数据加载与效果匹配 |
| `test_llm_fallback.py` | LLM 降级机制 |
| `test_pipeline.py` | 管线路由逻辑 |
| `test_query_executor.py` | 查询执行 |
| `test_sync_mooncell.py` | Mooncell 同步 |
| `test_translation.py` | 翻译层 |
| `test_guide_retriever.py` | 攻略检索 |

---

## 16. 关键设计决策

1. **两阶段路由而非端到端生成**：LLM 仅做意图理解，数据查询由确定性代码执行，保证准确性
2. **构建时预消化**：所有翻译、效果匹配、物化视图在构建时完成，运行时零额外开销
3. **声明式效果验证**：从 Chaldea Dart 源码提取规则，避免手写匹配逻辑，保持与游戏一致
4. **文档级 BM25 + 全文传入**：中期过渡架构，平衡召回率与 token 成本
5. **多供应商 LLM 容灾**：同供应商多模型 + 跨供应商降级，最大化可用性
6. **两层昵称系统**：Mooncell 自动同步（基础层）+ 手工覆盖（优先层），兼顾自动化与精确性
7. **Agent 工具调用兜底**：Pipeline B 使用 Agent 循环处理复杂/开放性问题，最多 8 轮
8. **SSE 流式输出**：用户感知延迟低，支持 thinking 步骤实时展示
