# Laplace

> AI Native 对话式 FGO 数据助手 —— 用自然语言查询 Fate/Grand Order 游戏数据

## 项目简介

Laplace 利用大语言模型（LLM）的意图识别能力，将传统的 FGO 工具软件转化为**对话式智能助手**。用户无需学习复杂的筛选 UI，只需用自然语言提问，即可获得精确的游戏数据。基于 **Schema Mirror** 架构，将 Chaldea Dart 核心领域知识无缝注入大模型。

**Old Way**: 打开 App → 选择从者列表 → 点击筛选 → 勾选各种条件组合

**Laplace**: 输入 "帮我找一下 30 自充的从者有哪些" 或 "有无敌技能的五星从者" → AI 直接返回结果

## 功能特性

- [x] 自然语言对话交互界面
- [x] **自研 DAG 图引擎（v0.5.0）** — `server/graph/` 提供约 280 行的 StateGraph，零外部依赖，将原硬编码三管线重构为声明式节点 + 条件边，支持流式与同步统一 API、节点级 trace_id、Checkpointer 持久化与 interrupt/resume 续跑（详见 [docs/architecture.md](docs/architecture.md#4-dag-图引擎与节点拓扑v050)）。
- [x] **多轮对话状态机（v0.5.0）** — turn_type 三类（MAJOR / MINOR / CORRECTION）+ SessionStore + SqliteCheckpointer（WAL 模式 + 30 分钟 TTL），MINOR 支持 4 种复用粒度（复用整套管线 / 追加过滤 / 切换 response_skill / 修正参数）。
- [x] **SSE 节点级实时推送（v0.5.0）** — 三套图实例（confirmation_stream / direct_stream / a_stream）统一从 `stream_chat_events` 入口分发；流式节点立即 yield，普通节点 pending_events 缓冲，6 种事件契约（thinking / routing / data / token / clarification / done）。
- [x] **三管线路由架构** — Stage 0 分类器自动分流到：Pipeline A（结构化技能链）/ Pipeline B（Atlas 知识 Agent 工具循环，最多 8 轮）/ Pipeline C（戴冠攻略 BM25 文档检索）。
- [x] **Skill-Based Architecture** — 19 个查询技能 + 6 个回复技能，覆盖 servant / ce / coronation 三个域，通过 `@register_skill` 装饰器零侵入注册。
- [x] **Atlas 知识 Agent** — Pipeline B 提供 7 个工具（search_servants / lookup_servant / list_effects / lookup_skill_detail 等）处理开放性机制询问，带事实核验。
- [x] **戴冠攻略检索** — Pipeline C 基于中文 bigram + BM25 的文档级评分 + Tag 过滤，命中文档全文传入 LLM。
- [x] LLM Skill 路由 — 自然语言 → Skill 调用组合（RoutingResponse JSON 契约）
- [x] Schema Mirror 架构 — 同步提取开源项目 Chaldea 的游戏效果领域知识
- [x] 全面从者查询 — 支持 30% NP 自充、55 种复杂技能效果（如无敌、毅力、加攻）、目标类型组合筛选
- [x] 从者与特性深度解析 — 性别、阵营、配卡、宝具颜色类型、特性（Trait，如秩序善）
- [x] 概念礼装（CE）查询 — 按名称/效果/稀有度/攻击血量类型/获取方式查找礼装
- [x] 从者别名系统 — 两层昵称（Mooncell 自动同步基础层 + 手工覆盖优先层）
- [x] 数据后端预消化 (Pre-digestion) — 所有英文枚举值构建时完成中文翻译，运行时零翻译开销，根除 LLM 翻译幻觉。
- [x] 全链路日志追踪 (Tracing) — JSONL 结构化日志，15 种 Phase 覆盖完整请求生命周期，支持通过 TraceID 回溯。
- [x] LLM Contract — 使用 JSON Schema + Pydantic 校验约束 Skill 路由输出，默认回归测试不消耗 LLM quota。
- [x] Thinking Steps 流式交互 — SSE 6 种事件类型（thinking / servants / clarification / delta / done / error）分阶段展示 AI 思考过程，从者卡片先行渲染。
- [x] **多供应商 LLM 容灾** — OpenAI / Obao / Dashscope 三个适配器，两层降级（同供应商多模型轮转 → 跨供应商降级）。
- [x] **Preset 快捷查询** — 前端提供「周回筛选」「从者查询」「从者对比」「辅助推荐」四个快捷入口。
- [x] **职阶克制查询** — 支持“克制伪装者的从者”等基于 Atlas API 克制关系数据的查询维度。
- [x] **空结果智能提示** — 筛选无匹配时明确列出已识别条件并给出放宽建议。
- [x] **监控与告警** — LLM 主动健康探测 + Prometheus 指标输出 + Bark / Telegram 双通道告警。

## 技术栈

| 类别 | 技术 |
| :--- | :--- |
| 前端 | HTML / Vanilla CSS / Vanilla JS（SSE 流式）|
| 后端 | Python 3.12 / FastAPI / Uvicorn / SSE / BM25 |
| 图引擎 | 自研 StateGraph（~280 行，零外部依赖，DAG + 流式 + Checkpointer）|
| 状态存储 | InMemoryCheckpointer + SqliteCheckpointer（WAL 模式 + 30 分钟 TTL）|
| LLM | 多供应商适配：OpenAI / Obao / Dashscope |
| 数据源 | Atlas Academy API（底层数据）+ Chaldea（领域知识）+ Mooncell Wiki（昵称） |

## 快速开始

### 环境要求

- Python 3.12+

### 安装与启动

```bash
# 1. 创建虚拟环境并激活
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装依赖
pip install -r server/requirements.txt

# 3. (可选) 安装开发依赖 — 如需运行 lint/test
pip install -e ".[dev]"

# 4. 配置 API Key
cp .env.example .env
# 编辑 .env 填入你的模型 API 密钥

# 5. 下载从者数据（首次运行必需）
python3 -m server.data_loader

# 6. 启动 FastAPI 服务端
python3 -m uvicorn server.main:app --reload

# 7. 打开前端界面
# 在浏览器中直接打开 demo/index.html 即可使用
```

> **部署 vs 开发**：纯部署只需步骤 1-2-4-5-6（`requirements.txt` 包含运行所需的全部依赖）。步骤 3 安装的 ruff + pytest 仅用于本地开发和代码检查。步骤 5 会从 Atlas Academy API 下载从者数据，生成 `server/data/servants_db.json`（该文件不纳入 git 版本控制）。

### 知识库与数据同步

系统包含一个独立的数据刷新管线：

```bash
source .venv/bin/activate

# 1. 解析 Chaldea 源码生成枚举与效果知识库 (Effect Schema)
python3 server/sync_chaldea.py

# 2. 根据知识库去 Atlas API 抓取从者全量数据
python3 -m server.data_loader
```

**Chaldea 依赖说明**：
- `chaldea-center/chaldea` **不是 runtime 强依赖**，仅在运行 `sync_chaldea.py` 更新领域知识时需要。
- 普通运行只依赖已生成的 `server/knowledge/*.json` 与 `server/data/servants_db.json`。
- 运行 `sync_chaldea.py` 时，脚本会**自动管理** Chaldea 源码：
  - 不存在 → 自动 `git clone --depth 1`（浅克隆节省磁盘）
  - 已存在 → 自动 `git pull` 更新到最新
- 支持通过 `CHALDEA_SRC_PATH` 环境变量指定自定义源码路径：
  ```bash
  export CHALDEA_SRC_PATH=/path/to/your/chaldea
  python3 server/sync_chaldea.py
  ```

**Effect Schema Overlay 机制**：
- `sync_chaldea.py` 只生成 `server/knowledge/effect_schema.json`（纯净的 Chaldea 领域知识），每次同步会**整体覆盖**此文件。
- 手工业务扩展（如虚拟复合效果 `damageBoost`/`damageShield`、翻译修正）存放在 `server/config/effect_overrides.json`，**不会被 sync 覆盖**。
- 系统在 runtime 自动将两层数据合并（overlay 同名效果优先覆盖），无需手动干预。
- 新增虚拟复合效果时，只需编辑 `server/config/effect_overrides.json`，无需修改代码。

### 代码检查与测试

```bash
source .venv/bin/activate

# 代码检查（需先安装开发依赖：pip install -e ".[dev]"）
ruff check server/ tests/ extractor/    # lint 检查
ruff format --check server/ tests/      # 格式检查（仅检查，不修改）
ruff format server/ tests/              # 自动格式化

# 默认回归测试（不访问网络、不调用 LLM）
python -m pytest

# 编译检查
python -m compileall -q server extractor

# 真实 LLM JSON Schema smoke test（会消耗少量 quota）
RUN_LIVE_LLM_TESTS=1 python -m pytest tests/test_llm_client_live.py -s
```

当前 LLM smoke test 会输出本次 `json_mode=True` 的实际路径：`json_schema` 表示网关原生支持 `response_format/json_schema`，`text_fallback` 表示自动降级到普通 JSON 文本解析后成功。

> **CI 自动化**：每次 push 到 main 或提交 PR，GitHub Actions 会自动运行 ruff check + pytest。结果可在仓库的 [Actions](../../actions) 页面查看。

### 环境变量配置

复制 `.env.example` 为 `.env` 并填入真实密钥：

```bash
cp .env.example .env
```

#### LLM 多提供商配置

支持配置多个 LLM 提供商，按优先级自动降级。降级策略为两层：同提供商内按模型列表顺序降级，全部失败后切换下一个提供商。

```bash
# 提供商降级链（按优先级排列，逗号分隔）
LLM_PROVIDERS=obao,openai

# 每个提供商的配置（命名约定：LLM_{NAME}_URL / LLM_{NAME}_KEY / LLM_{NAME}_MODELS）
LLM_OBAO_URL=https://api.obao.cloud/v1
LLM_OBAO_KEY=your-obao-api-key
LLM_OBAO_MODELS=claude-sonnet-4-6,Deepseek-V4-Flash,gpt-5.4

LLM_OPENAI_URL=https://api.openai.com/v1
LLM_OPENAI_KEY=your-openai-api-key
LLM_OPENAI_MODELS=gpt-4o,gpt-4o-mini
```

上例的降级链为：`obao/claude-sonnet` → `obao/Deepseek` → `obao/gpt-5.4` → `openai/gpt-4o` → `openai/gpt-4o-mini`。

> **向后兼容**：未配置 `LLM_PROVIDERS` 时，自动回退旧变量 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_FALLBACK_MODELS`，零迁移成本。

#### 其他环境变量

| 变量 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `CORS_ORIGINS` | CORS 白名单（逗号分隔） | `http://localhost:8000,http://127.0.0.1:8000` |
| `RATE_LIMIT_PER_MINUTE` | 单 IP 每分钟最大请求数 | `10` |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | 全站每分钟最大请求数（0=不限） | `100` |
| `CHALDEA_SRC_PATH` | Chaldea 源码路径（仅 sync 时使用） | `chaldea-center/chaldea` |
| `ADMIN_PASSWORD_HASH` | 管理员密码 SHA256 哈希（后台登录） | （空，未设置时不可登录） |
| `CONTAINER_NAME` | Docker 容器名称（后台重启功能） | `laplace` |
| `BARK_URL` | Bark 推送 URL（iOS 告警主通道） | （空，不启用 Bark） |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（告警备选通道） | （空） |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID（告警接收者） | （空） |
| `ALERT_CONSECUTIVE_THRESHOLD` | 连续失败触发告警阈值 | `5` |
| `MONITOR_PROBE_INTERVAL` | LLM 健康探测间隔（秒，0=禁用） | 代码内默认 |

> **开发机提示**：开发机本地 `.env` **不应该包含真实的** `BARK_URL` / `TELEGRAM_*`，否则本机跑测试/启动 server 时会向运维通道误发告警。这三个凭据应只在生产/部署侧的 `.env` 中设置。

> **本地开发提示**：如果在其他设备上测试时 uvicorn 绑定了非默认地址（如 `http://192.168.x.x:8000`），需要将该地址添加到 `CORS_ORIGINS` 中，否则浏览器会因 CORS 策略拦截请求。示例：
> ```bash
> CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.100:8000
> ```

### Docker 部署

适用于将 Laplace 部署到云服务器供其他玩家使用。

```bash
# 1. 构建镜像（BUILD_VERSION 用于自动检测数据是否需要重建）
docker build --build-arg BUILD_VERSION=$(git rev-parse --short HEAD) -t laplace:latest .

# 2. 准备 .env 文件（填入 LLM API Key 等配置）
cp .env.example .env
# 编辑 .env 填入真实密钥

# 3. 启动容器
docker run -d \
  --name laplace \
  --env-file .env \
  -p 8000:8000 \
  -v laplace-logs:/app/server/logs \
  --restart unless-stopped \
  laplace
```

容器首次启动时会自动从 Atlas Academy 下载从者数据（约 30 秒）。后续重建镜像后，entrypoint 会通过版本戳自动检测代码是否更新，如有变更则重建数据库，无变更则跳过。

**常用操作**：

```bash
# 查看日志
docker logs -f laplace

# 强制刷新从者数据
docker run -d --env-file .env -e REFRESH_DATA_ON_START=1 -p 8000:8000 laplace

# 更新部署（拉取最新代码后）
docker build --build-arg BUILD_VERSION=$(git rev-parse --short HEAD) -t laplace:latest . && docker rm -f laplace && docker run -d --name laplace --env-file .env -p 8000:8000 -v laplace-logs:/app/server/logs -v $(pwd)/.env:/app/.env:ro --restart unless-stopped laplace:latest
```

**Nginx 反向代理**：生产环境建议在容器前加 Nginx 处理 SSL 和静态文件托管。参考配置见 `deploy/nginx.conf`。注意以下关键配置：
- SSE 流式响应需要 `proxy_buffering off`
- `/admin` 路径必须代理到后端（admin 后台页面和 API 由 FastAPI 统一托管）
- `/api/` 路径代理到后端（业务 API）
- 完整部署脚本参考 `deploy/deploy.example.sh`

**管理后台**：Laplace 提供了 admin 管理后台（`/admin/`），支持环境变量编辑、配置文件管理、日志查看等功能。使用前需在 `.env` 中配置 `ADMIN_PASSWORD_HASH`（SHA256 哈希值）。生成方式：
```bash
echo -n "your-password" | sha256sum | awk '{print $1}'
```

> **Docker 环境变量补充**：除 `.env` 中的变量外，容器还支持以下额外变量：
>
> | 变量 | 说明 | 默认值 |
> | :--- | :--- | :--- |
> | `REFRESH_DATA_ON_START` | 启动时强制重新下载从者数据 | `0` |
> | `UVICORN_WORKERS` | uvicorn worker 进程数 | `1` |

## 项目结构

```
Laplace/
├── README.md              # 项目主页
├── SOUL.md / AGENTS.md / USER.md / MEMORY.md  # AI 系统级 Prompt 与记忆
├── 需求描述.md             # 详细需求与架构规划
├── docs/                   # 架构文档
│   ├── architecture.md             # 项目架构总览（基于源码生成）
│   ├── adr/ + architecture-discussions/  # ADR 决策记录
│   └── 产品/部署/展示等补充文档
├── demo/                   # Web 前端（SSE 流式 + 卡片渲染）
│   ├── index.html
│   ├── style.css
│   └── app.js
├── admin/                  # 后台管理界面静态资源（index.html / admin.css / admin.js）
├── server/                 # Python FastAPI 后端
│   ├── main.py             # FastAPI 入口（CORS / RateLimit / 路由注册 / SSE 入口 stream_chat_events）
│   ├── pipeline.py         # 图实例编排与缓存（Pipeline A/B/C + Direct + Confirmation 共 7 个图）
│   ├── edges.py            # DAG 条件边路由函数（after_classify / after_route / _dispatch_bail_out 等）
│   ├── graph/              # 自研 StateGraph 图引擎（v0.5.0）
│   │   ├── engine.py       # ~280 行 StateGraph 核心（add_node / add_stream_node / run / run_stream / resume）
│   │   ├── state.py        # GraphState TypedDict（query / skill_calls / turn_type / prev_turn 等）
│   │   ├── checkpointer.py # InMemoryCheckpointer + SqliteCheckpointer（WAL + 30 分钟 TTL）
│   │   ├── session.py      # SessionStore：thread_id 维度的多轮会话管理
│   │   └── decorators.py   # @traced_node / @async_traced_node（节点级 trace_id 注入）
│   ├── nodes/              # DAG 图节点目录（11 个节点单一职责）
│   │   ├── classify.py     # Stage 0 分类器节点（A/B/C 链路 + turn_type 判定）
│   │   ├── route.py        # Pipeline A 路由节点（LLM OneShot 解析 SkillCall 列表）
│   │   ├── merge_filters.py # MINOR/CORRECTION 多轮节点（复用 prev_turn 参数）
│   │   ├── execute.py      # Skill 执行节点（SkillExecutor 调度）
│   │   ├── generate.py     # Pipeline A 生成节点（流式 yield token）
│   │   ├── atlas.py        # Pipeline B Atlas 索引节点（流式）
│   │   ├── guide.py        # Pipeline C 攻略检索节点（流式）
│   │   ├── agent_fallback.py # Agent Tool-Use 降级节点（流式）
│   │   ├── clarify.py      # Clarification 节点（interrupt 等待用户确认）
│   │   ├── template_fallback.py # 模板兜底节点
│   │   └── direct_response.py # preset_name / confirmation_id 直达节点
│   ├── prompts.py          # 分类器 / 路由器 / 生成 Prompt 模板
│   ├── schemas.py          # RoutingResponse / 工具结果 Pydantic 契约
│   ├── context_builder.py  # LLM 上下文构建（从者/CE 详情格式化）
│   ├── translation.py      # 职阶中英映射、效果翻译、过滤条件中文化
│   ├── query_executor.py   # 数据加载 + 效果匹配 + 昵称解析
│   ├── guide_retriever.py  # Pipeline C 戴冠攻略 BM25 检索
│   ├── data_loader.py      # 从 Atlas API 构建从者/CE 数据库
│   ├── sync_chaldea.py     # 从 Chaldea Dart 源码同步 Schema 知识
│   ├── fallback.py         # Agent 标签解析（GREETING/OUT_OF_SCOPE/UNSUPPORTED）
│   ├── face_proxy.py       # 从者头像反代
│   ├── logger.py           # JSONL 结构化日志（15 种 Phase）
│   ├── rate_limiter.py     # 滑动窗口速率限制中间件
│   ├── llm/                # LLM 多适配器（base / openai / obao / dashscope / provider）
│   ├── skills/             # Skill-Based Architecture 模块
│   │   ├── base.py         # QuerySkill / ResponseSkill 基类 + @register_skill
│   │   ├── executor.py     # 技能执行器（域分组 + AND 合并 + 四级降级）
│   │   ├── presets.py      # 4 个 Preset 快捷查询定义
│   │   ├── query/          # 19 个查询技能（servant / ce / coronation 三个域）
│   │   └── response/       # 6 个回复技能
│   ├── agent/              # Pipeline B Agent 系统（agent_loop / tool_defs / tool_handlers）
│   ├── admin/              # Admin 路由（routes.py）+ 认证（auth.py）
│   ├── monitor/            # 监控（metrics + alerter + health_checker）
│   ├── data/               # 生成的数据库（servants_db / craft_essences_db / atlas_index / guides/）
│   ├── config/             # 可热更新配置（昵称、术语别名、效果覆盖、戴冠攻略元数据）
│   └── knowledge/          # Chaldea 派生知识（class_mapping / effect_schema / mappings）
├── extractor/              # 数据提取脚本（sync_mooncell_nicknames.py / np_charge_filter.py）
├── tests/                  # pytest 回归测试
└── chaldea-center/         # Chaldea 参考源码（可选，仅 sync_chaldea.py 需要）
```

**可选目录说明**：
- `chaldea-center/` — 仅在需要更新领域知识时存在，普通运行不需要
- `extractor/` — 独立的数据提取脚本集（如 Mooncell 昵称同步），不参与 runtime

## 如何新增 Skill

Skill-Based Architecture 将查询逻辑拆分为独立模块，新增查询维度（如按礼装、按素材）或分析模板只需以下步骤。

### 新增 Query Skill（查询类）

以"按礼装筛选"为例，需要修改 **4 个文件**，新建 **1 个文件**：

#### 1. 创建 Skill 模块

新建 `server/skills/query/search_by_craft_essence.py`：

```python
"""Skill: 按礼装筛选从者。"""

from pydantic import BaseModel
from server.skills.base import QuerySkill, register_skill


class Params(BaseModel):
    """参数模型 — Pydantic 自动校验，校验失败会跳过该 Skill。"""
    ce_name: str  # 礼装名称


@register_skill
class SearchByCraftEssence(QuerySkill):
    name = "search_by_craft_essence"          # 唯一标识，LLM 路由使用
    description = "按礼装名称筛选从者"          # LLM 路由时的能力描述
    domain = "servant"                         # 数据域（servant / ce / coronation）

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def filter(self, servant: dict, params: dict) -> bool:
        """单从者匹配逻辑。返回 True 表示命中。"""
        ce_name = params.get("ce_name", "")
        # 实现你的筛选逻辑...
        return ce_name.lower() in str(servant.get("recommendCE", "")).lower()
```

**关键约定**：
- `@register_skill` 装饰器自动将 Skill 实例注册到全局 `SKILL_REGISTRY`
- `name` 必须唯一，LLM 路由结果中会引用此名称
- `description` 会被注入 LLM 路由 Prompt，描述越清晰，路由越准确
- `params_schema` 返回 Pydantic 模型，`SkillExecutor` 会自动校验参数
- `filter()` 是核心匹配逻辑，对数据库中每个从者调用一次
- 如果需要自定义执行逻辑（如不是简单 filter），可以重写 `execute(db, params)` 方法

#### 2. 注册模块导入

在 `server/skills/__init__.py` 的 `_SKILL_MODULES` 列表中追加一行：

```python
_SKILL_MODULES = [
    # Query Skills
    ...
    "server.skills.query.search_by_craft_essence",  # 新增
    # Response Skills
    ...
]
```

#### 3. 前端 Skill 中文名映射

在 `demo/app.js` 的 `SKILL_DISPLAY_NAMES` 中追加一行：

```javascript
const SKILL_DISPLAY_NAMES = {
  ...
  search_by_craft_essence: "礼装筛选",  // 新增
};
```

#### 4. 回归测试

在 `tests/test_skill_framework.py` 中为新 Skill 补充单元测试。

---

### 新增 Response Skill（分析模板）

以"从者编队推荐"为例：

#### 1. 创建 Response Skill 模块

新建 `server/skills/response/respond_team_recommendation.py`：

```python
"""Response Skill: 编队推荐分析。"""

from server.skills.base import ResponseSkill, register_skill


@register_skill
class RespondTeamRecommendation(ResponseSkill):
    name = "respond_team_recommendation"
    description = "根据筛选结果推荐编队搭配"

    def build_prompt(self, user_message: str, context_json: str) -> str:
        return (
            "你是 FGO 编队搭配专家。用户的问题是：\n"
            f"「{user_message}」\n\n"
            f"以下是候选从者数据：\n{context_json}\n\n"
            "请根据从者的技能效果和宝具类型，推荐 1-2 个编队方案。"
        )
```

#### 2. 注册模块导入

同样在 `server/skills/__init__.py` 的 `_SKILL_MODULES` 列表中追加。

---

### 新增 Preset（快捷查询）

如果希望新 Skill 也有前端快捷入口，还需修改 **2 个额外文件**：

#### 1. 后端注册 Preset

在 `server/skills/presets.py` 中追加：

```python
Preset(
    name="ce_search",
    display_name="礼装筛选",
    query_skills=["search_by_craft_essence"],
    response_skill="respond_servant_list",
    param_template={
        "search_by_craft_essence": {"ce_name": "黑圣杯"},  # 默认参数
    },
),
```

#### 2. 前端注册 Preset

在 `demo/app.js` 的 `PRESETS` 数组中追加对应条目。

---

### Checklist 速查

| 步骤 | 文件 | 操作 |
|:-----|:-----|:-----|
| **1. 创建 Skill** | `server/skills/query/<name>.py` 或 `response/<name>.py` | 新建，实现 `filter()` 或 `build_prompt()` |
| **2. 注册导入** | `server/skills/__init__.py` | 追加模块路径到 `_SKILL_MODULES` |
| **3. 前端中文名** | `demo/app.js` → `SKILL_DISPLAY_NAMES` | 追加 `skill_name: "中文名"` |
| **4. 单元测试** | `tests/test_skill_framework.py` | 补充 filter/execute 测试 |
| **5. (可选) Preset** | `server/skills/presets.py` + `demo/app.js` → `PRESETS` | 注册快捷入口 |

> **注意**：无需修改 `server/main.py`、`server/prompts.py` 或路由逻辑。Skill 的 `description` 字段会被自动注入 LLM 路由 Prompt，`@register_skill` 装饰器自动完成注册，`SkillExecutor` 自动识别并执行新 Skill。

## 支持项目

Laplace 是一个纯个人开源项目，不做商业化、不会收费。如果它帮到了你，可以考虑请作者喝杯咖啡，所有收入将用于 API 调用和服务器运行成本。

- **爱发电**：[https://afdian.com/a/laplace-fgo](https://afdian.com/a/laplace-fgo)
- **微信 / 支付宝**：扫描下方收款码

<p align="center">
  <img src="demo/微信收款码.JPG" alt="微信收款码" width="200">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="demo/支付宝收款码.JPG" alt="支付宝收款码" width="200">
</p>

## 合规声明

数据及部分领域逻辑源自开源项目 [Chaldea](https://github.com/chaldea-center/chaldea)，数据来源 [Atlas Academy](https://atlasacademy.io/)。

## License

CC-BY-NC-SA-4.0
