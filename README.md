# Laplace

> AI Native 对话式 FGO 数据助手 —— 用自然语言查询 Fate/Grand Order 游戏数据

## 项目简介

Laplace 利用大语言模型（LLM）的意图识别能力，将传统的 FGO 工具软件转化为**对话式智能助手**。用户无需学习复杂的筛选 UI，只需用自然语言提问，即可获得精确的游戏数据。基于 **Schema Mirror** 架构，将 Chaldea Dart 核心领域知识无缝注入大模型。

**Old Way**: 打开 App → 选择从者列表 → 点击筛选 → 勾选各种条件组合

**Laplace**: 输入 "帮我找一下 30 自充的从者有哪些" 或 "有无敌技能的五星从者" → AI 直接返回结果

## 功能特性

- [x] 自然语言对话交互界面
- [x] AI Native 生成式响应 — **(新)** Two-Step RAG 架构。大模型不仅能检索数据，还能真正“看到”数据并进行拟人化总结回答。
- [x] LLM 意图解析 — 自然语言 → 结构化 JSON 查询指令
- [x] Schema Mirror 架构 — 同步提取开源项目 Chaldea 的游戏效果领域知识
- [x] 全面从者查询 — 支持 30% NP 自充、55 种复杂技能效果（如无敌、毅力、加攻）、目标类型组合筛选
- [x] 从者与特性深度解析 — 性别、阵营、配卡、宝具颜色类型、特性（Trait，如秩序善）
- [x] 从者别名系统 — 自动拉取最新的社区别名与中文词典
- [x] **(新)** 数据后端预消化 (Pre-digestion) — 彻底根除大模型翻译幻觉，节省 Token。
- [x] **(新)** 全链路日志追踪 (Tracing) — 支持通过 TraceID 回溯每一条查询的原始解析状态。
- [x] **(新)** LLM Contract — 使用 JSON Schema + Pydantic 校验约束意图解析输出，默认回归测试不消耗 LLM quota。
- [x] **(新)** Thinking Steps 流式交互 — SSE 分阶段展示 AI 思考过程（解析→检索→生成），从者卡片先行渲染，零额外 Token 消耗。

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

# 3. 配置 API Key
cp .env.example .env
# 编辑 .env 填入你的模型 API 密钥

# 4. 启动 FastAPI 服务端
python3 -m uvicorn server.main:app --reload

# 5. 打开前端界面
# 在浏览器中直接打开 demo/index.html 即可使用
```

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
- 如需重新同步知识库，从 https://github.com/chaldea-center/chaldea.git 拉取 Chaldea 源码到 `chaldea-center/chaldea` 目录。
- 支持通过 `CHALDEA_SRC_PATH` 环境变量指定 Chaldea 源码路径：
  ```bash
  export CHALDEA_SRC_PATH=/path/to/your/chaldea
  python3 server/sync_chaldea.py
  ```

### 测试

```bash
source .venv/bin/activate

# 默认回归测试（不访问网络、不调用 LLM）
python -m pytest

# 编译检查
python -m compileall -q server extractor

# 真实 LLM JSON Schema smoke test（会消耗少量 quota）
RUN_LIVE_LLM_TESTS=1 python -m pytest tests/test_llm_client_live.py -s
```

当前 LLM smoke test 会输出本次 `json_mode=True` 的实际路径：`json_schema` 表示网关原生支持 `response_format/json_schema`，`text_fallback` 表示自动降级到普通 JSON 文本解析后成功。

### 环境变量配置

在 `.env` 文件中可配置以下选项：

| 变量 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `LLM_BASE_URL` | LLM API 基础地址 | - |
| `LLM_API_KEY` | LLM API 密钥 | - |
| `LLM_MODEL` | 主模型 | - |
| `LLM_FALLBACK_MODELS` | 回退模型（逗号分隔） | - |
| `CORS_ORIGINS` | CORS 白名单（逗号分隔） | `http://localhost:8000,http://127.0.0.1:8000` |
| `RATE_LIMIT_PER_MINUTE` | 单 IP 每分钟最大请求数 | `10` |
| `RATE_LIMIT_GLOBAL_PER_MINUTE` | 全站每分钟最大请求数（0=不限） | `100` |

> **本地开发提示**：如果在其他设备上测试时 uvicorn 绑定了非默认地址（如 `http://192.168.x.x:8000`），需要将该地址添加到 `CORS_ORIGINS` 中，否则浏览器会因 CORS 策略拦截请求。示例：
> ```bash
> CORS_ORIGINS=http://localhost:8000,http://127.0.0.1:8000,http://192.168.1.100:8000
> ```

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
│   ├── main.py            # API 入口
│   ├── llm_client.py      # 大模型交互客户端
│   ├── schemas.py         # LLM 意图解析 JSON Schema / Pydantic Contract
│   ├── prompts.py         # System Prompt 模板与组装
│   ├── query_executor.py  # 核心查询执行器
│   ├── data_loader.py     # 从者数据提取构建
│   ├── sync_chaldea.py    # Schema Mirror 领域知识解析器
│   ├── data/              # 生成的从者数据库
│   └── knowledge/         # 提取的 JSON 格式领域知识
├── tests/                 # pytest 回归测试
└── chaldea-center/        # Chaldea 参考源码（可选，仅 sync_chaldea.py 需要）
```

**可选目录说明**：
- `chaldea-center/` — 仅在需要更新领域知识时存在，普通运行不需要
- `extractor/` — 早期 NP 充能筛选器原型，已迁移至 `server/data_loader.py`，保留仅用于向后兼容

## 合规声明

数据及部分领域逻辑源自开源项目 [Chaldea](https://github.com/chaldea-center/chaldea)，数据来源 [Atlas Academy](https://atlasacademy.io/)。

## License

CC-BY-NC-SA-4.0
