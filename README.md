# Laplace

> AI Native 对话式 FGO 数据助手 —— 用自然语言查询 Fate/Grand Order 游戏数据

## 项目简介

Laplace 利用大语言模型（LLM）的意图识别能力，将传统的 FGO 工具软件转化为**对话式智能助手**。用户无需学习复杂的筛选 UI，只需用自然语言提问，即可获得精确的游戏数据。基于 **Schema Mirror** 架构，将 Chaldea Dart 核心领域知识无缝注入大模型。

**Old Way**: 打开 App → 选择从者列表 → 点击筛选 → 勾选各种条件组合

**Laplace**: 输入 "帮我找一下 30 自充的从者有哪些" 或 "有无敌技能的五星从者" → AI 直接返回结果

## 功能特性

- [x] 自然语言对话交互界面
- [x] AI Native 生成式响应 — **(新)** Two-Step RAG 架构。大模型不仅能检索数据，还能真正“看到”数据并进行拟人化总结回答。
- [x] LLM Skill 路由 — 自然语言 → Skill 调用组合（RoutingResponse JSON 契约）
- [x] Schema Mirror 架构 — 同步提取开源项目 Chaldea 的游戏效果领域知识
- [x] 全面从者查询 — 支持 30% NP 自充、55 种复杂技能效果（如无敌、毅力、加攻）、目标类型组合筛选
- [x] 从者与特性深度解析 — 性别、阵营、配卡、宝具颜色类型、特性（Trait，如秩序善）
- [x] 从者别名系统 — 自动拉取最新的社区别名与中文词典
- [x] 数据后端预消化 (Pre-digestion) — 彻底根除大模型翻译幻觉，节省 Token。
- [x] 全链路日志追踪 (Tracing) — 支持通过 TraceID 回溯每一条查询的原始解析状态。
- [x] LLM Contract — 使用 JSON Schema + Pydantic 校验约束 Skill 路由输出（RoutingResponse），默认回归测试不消耗 LLM quota。
- [x] Thinking Steps 流式交互 — SSE 分阶段展示 AI 思考过程（解析→检索→生成），从者卡片先行渲染，零额外 Token 消耗。
- [x] **(新)** Skill-Based Architecture — 两阶段 LLM 路由（Stage 1 路由选 Skill → Stage 2 精填参数），查询逻辑拆分为独立 Skill 模块，可扩展性大幅提升。
- [x] **(新)** Preset 快捷查询 — 前端提供「周回筛选」「从者查询」「从者对比」「辅助推荐」四个快捷入口，一键触发预定义查询流程。

## 技术栈

| 类别 | 技术 |
| :--- | :--- |
| 前端 | HTML / Vanilla CSS / Vanilla JS |
| 后端 | Python (FastAPI, Uvicorn) |
| LLM | API 兼容模型 (如 Claude/Deepseek) 托管于 Obao Cloud |
| 数据源 | Atlas Academy API (底层数据) + Chaldea (领域知识) |

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
LLM_OBAO_URL=https://x.obao.cloud/v1
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

> **本地开发提示**：如果在其他设备上测试时 uvicorn 绑定了非默认地址（如 `http://192.168.x.x:8000`），需要将该地址添加到 `CORS_ORIGINS` 中，否则浏览器会因 CORS 策略拦截请求。示例：
> ```bash
> CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.100:8000
> ```

### Docker 部署

适用于将 Laplace 部署到云服务器供其他玩家使用。

```bash
# 1. 构建镜像
docker build -t laplace .

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

容器首次启动时会自动从 Atlas Academy 下载从者数据（约 30 秒）。后续重启会跳过下载。

**常用操作**：

```bash
# 查看日志
docker logs -f laplace

# 强制刷新从者数据
docker run -d --env-file .env -e REFRESH_DATA_ON_START=1 -p 8000:8000 laplace

# 更新部署（拉取最新代码后）
docker build -t laplace . && docker rm -f laplace && docker run -d --name laplace --env-file .env -p 8000:8000 -v laplace-logs:/app/server/logs --restart unless-stopped laplace
```

**Nginx 反向代理**：生产环境建议在容器前加 Nginx 处理 SSL 和静态文件托管。参考配置见 `deploy/nginx.conf`。注意 SSE 流式响应需要 `proxy_buffering off`。

**Cloudflare 反代说明**：如果 LLM API 提供商（如 obao）前端有 Cloudflare 防护，服务器直接请求可能返回 403。解决方案是在 Nginx 中添加 `/llm-proxy/` location，注入浏览器 User-Agent 绕过 Bot 检测，然后将 `.env` 中的 `LLM_BASE_URL` 指向本地反代（如 `http://172.17.0.1/llm-proxy/v1`）。详见 [部署指南 - 问题排查](docs/DEPLOYMENT.md#问题-4-llm-api-返回-403-cloudflare-拦截)。

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
├── SOUL.md / AGENTS.md / USER.md / MEMORY.md # AI 系统级 Prompt 与记忆
├── 需求描述.md             # 详细需求与架构规划
├── demo/                  # Web 前端界面
│   ├── index.html
│   ├── style.css
│   └── app.js
├── server/                # Python FastAPI 后端
│   ├── main.py            # API 入口（Skill 模式路由）
│   ├── llm_client.py      # 大模型交互客户端（通用 schema/validator 接口）
│   ├── schemas.py         # RoutingResponse Pydantic 契约
│   ├── prompts.py         # Skill 路由 Prompt 模板
│   ├── query_executor.py  # 共享工具函数（load_database / 昵称解析）
│   ├── data_loader.py     # 从者数据提取构建
│   ├── sync_chaldea.py    # Schema Mirror 领域知识解析器
│   ├── skills/            # Skill-Based Architecture 模块
│   │   ├── __init__.py    # Skill 注册表
│   │   ├── executor.py    # Skill 执行引擎
│   │   ├── presets.py     # Preset 快捷查询定义
│   │   ├── query/         # 查询类 Skill（search_by_class / rarity / np_charge 等）
│   │   └── response/      # 响应类 Skill（servant_list / detail / compare 等）
│   ├── data/              # 生成的从者数据库
│   └── knowledge/         # 提取的 JSON 格式领域知识
├── tests/                 # pytest 回归测试
└── chaldea-center/        # Chaldea 参考源码（可选，仅 sync_chaldea.py 需要）
```

**可选目录说明**：
- `chaldea-center/` — 仅在需要更新领域知识时存在，普通运行不需要
- `extractor/` — 早期 NP 充能筛选器原型，已迁移至 `server/data_loader.py`，保留仅用于向后兼容

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
    domain = "servant"                         # 数据域（目前只有 servant）

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

## 合规声明

数据及部分领域逻辑源自开源项目 [Chaldea](https://github.com/chaldea-center/chaldea)，数据来源 [Atlas Academy](https://atlasacademy.io/)。

## License

CC-BY-NC-SA-4.0
