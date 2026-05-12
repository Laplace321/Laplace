## Laplace v0.2.6 产品更新日志

### 更新时间：2026 年 5 月

---

### 一、新增功能

**后台管理系统**

新增了 `/admin/` 后台管理页面，支持密码登录认证，提供以下管理能力：

- **环境变量管理**：查看当前生效的 LLM 提供商配置、CORS 白名单、速率限制等运行时参数（密钥脱敏显示）。
- **配置文件热更新**：在线编辑昵称映射（`nicknames.json`）、效果覆写（`effect_overrides.json`）等配置文件，保存后立即生效，无需重启服务。
- **日志查看**：查询历史对话 Trace 日志，支持按时间范围和关键词筛选，点击查看单条 Trace 的完整多阶段详情。
- **容器管理**：一键重启 Docker 容器（需配置 `CONTAINER_NAME` 环境变量）。

**从者特性系统增强（traitAdd 合并）**

修复了从者特性数据不完整的问题。之前的 `servants_db.json` 仅包含基础 `traits`，缺少 `traitAdd`（无条件附加特性）和 `ascensionAdd.individuality`（灵基覆盖特性）。现在：

- 搜索「兽科从者」能正确找到豹人（No.148），之前因缺少 traitAdd 中的兽科特性（2821）而遗漏。
- 搜索「兽科且活人」返回 3 个从者（含豹人），之前返回 0 个。
- 支持按灵基阶段筛选特性。例如梅露辛在灵基 0-2 拥有「圆桌骑士」特性，灵基 3-4 则没有。可以通过指定灵基阶段进行精确筛选。
- 约 60 个从者存在灵基间特性差异，7 个从者拥有条件触发特性（如关卡通关后获得）。

---

### 二、架构优化

**LLM 适配器架构重构**

将原来 700+ 行的单体 `llm_client.py` 拆分为多文件适配器架构（`server/llm/`）：

- `base.py`：定义 `BaseLLMAdapter` 抽象基类。
- `adapters/openai_adapter.py`、`dashscope_adapter.py`、`obao_adapter.py`：各家 SDK 的独立适配器实现。
- `provider.py`：提供商配置解析与降级调度。

新增 LLM 提供商只需新建一个 adapter 文件，无需修改调度逻辑。

**.env 加载统一化**

将 `.env` 文件的加载逻辑统一到 `server/main.py` 入口，使用 `python-dotenv` 的 `load_dotenv()` 替代之前分散在各子模块的手写 parser。子模块不再自行加载 `.env`，消除了重复加载和 E402 lint 警告。

---

### 三、问题修复

**Agent fallback 日志修复**

修复了 Agent 兜底路径下 Trace 日志缺少 `reply` 字段的问题。之前通过 Agent 路径回答的查询，在日志查看页面无法显示回复内容。现在 6 处 Agent 日志点位均记录完整回复。

**Obao Claude thinking 防护**

为 Obao 适配器的 `agent_completion()` 显式关闭 Claude thinking 模式，防止 Agent 工具调用场景下 thinking 输出干扰 JSON 解析。

---

### 四、部署变更

**新增环境变量**

| 变量 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `ADMIN_PASSWORD_HASH` | 管理员密码的 SHA256 哈希值 | （空，未设置时后台登录不可用） |
| `CONTAINER_NAME` | Docker 容器名称（后台重启容器功能需要） | `laplace` |

**Dockerfile 变更**

- 新增 `COPY admin/ admin/`，将后台管理前端页面打包进镜像。

**docker-entrypoint.sh 变更**

- 新增从挂载的 `/app/.env` 文件加载环境变量的逻辑，支持 volume 挂载模式传递配置。
