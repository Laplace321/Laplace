# Agents — 操作指南与全局约束

## 核心原则

1. **先读后写**: 修改任何文件前，先阅读并理解其当前内容和上下文
2. **最小变更**: 每次修改只做必要的改动，避免不相关的重构
3. **保持文档同步**: 代码变更后及时更新相关文档
4. **主动标准演进 (Proactive Standards)**: 在开发过程中，如果发现某种架构模式或优化手段具有通用价值（如预消化、日志追踪），必须主动向用户提议将其固化为“工程标准”，并更新至 `AGENTS.md`，而非被动等待指令。

## 代码规范

### 通用规则

- 使用有意义的变量和函数命名
- 函数保持单一职责，控制在合理长度内
- 错误处理不可省略
- 避免硬编码，使用配置或常量

### 提交规范

1. **遵循 Conventional Commits 格式**：
```
<type>(<scope>): <description>

[optional body]
```
类型包括：`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`

2. **Push 前必须通过三步本地验证**：
> **[非常重要]** 每次 `git commit` 前，必须依次执行以下三步验证，全部通过后才能 commit + push：
> ```bash
> source .venv/bin/activate                   # 先激活虚拟环境！
> ruff check server/ tests/ extractor/       # lint 检查
> ruff format --check server/ tests/          # 格式检查
> python -m pytest                            # 回归测试
> ```
> 缺少任何一步都可能导致 CI 红掉。`ruff check` 和 `ruff format` 是两个独立检查，不可互相替代。

3. **强制同步远程代码库**：
> **[非常重要]** 所有的本地 `git commit` 动作完成后，必须立即执行 `git push`（或 `git push origin main`）将代码推送到 GitHub 远程仓库，除非当时明确处于断网或实验性分支。不要只把代码留在本地！

## 工作流程

### 会话启动时

> **每次新会话开始时，必须按顺序执行以下步骤，再响应用户请求：**

1. 阅读 `SOUL.md` —— 加载身份、个性和行为约束
2. 阅读 `MEMORY.md`（热层索引，~50 行）—— 获取当前迭代计划、活跃问题、技术备忘和 ADR 索引
3. 阅读 `USER.md` —— 了解用户的技术偏好和沟通风格
4. 阅读 `需求描述.md` —— 理解项目的核心需求和目标
5. **按需深入**：如果当前任务涉及特定架构决策，根据 `MEMORY.md` 中的 ADR 索引表，读取对应的 `docs/adr/ADR-NNN-*.md`
6. `docs/CHANGELOG.md` 仅在需要回顾项目历史进度时阅读，日常不加载

### 接到新任务时

1. 阅读需求，确认理解无误
2. 检查 `MEMORY.md` 热层中是否有相关背景；如涉及特定架构，按 ADR 索引读取对应温层文件
3. 判断任务类型，按对应管线执行：

#### 管线 A：非架构相关任务（常规需求 / Bug 修复）

```
讨论方案 → 执行方案 → 更新 需求描述.md → 更新 MEMORY.md 和 docs/adr/ → (如有标准需沉淀) 更新 AGENTS.md
```

1. 与用户讨论并确认实现方案
2. 实现代码，验证功能正确性
3. 更新 `需求描述.md`（产品路线图状态、Phase 进度等）
4. 更新 `MEMORY.md` 热层（迭代计划、活跃问题、技术备忘）；如产生新决策，创建 `docs/adr/ADR-NNN-<slug>.md` 并在索引表追加一行
5. 如果完成了 Phase 或核心特性，在 `docs/CHANGELOG.md` 追加里程碑
6. 如果发现了新的通用架构约束或工程标准，主动向用户提议后更新 `AGENTS.md`
7. 按需更新 `README.md`（对外说明变更）、`PRODUCT.md`（用户侧功能变更）

#### 管线 B：架构相关重大决策

```
讨论方案 → 记录在 architecture-discussions → 继续讨论直至达成结论 → 更新 需求描述.md → 更新 MEMORY.md 和 docs/adr/ → (如有标准需沉淀) 更新 AGENTS.md
```

1. 与用户讨论架构方案
2. 在 `docs/architecture-discussions/` 创建讨论文档，记录方案对比、成本评估、Trade-off 分析
3. 持续讨论，补充评估结果，直至与用户**达成明确结论**
4. 结论确定后，更新 `需求描述.md`
5. 在 `docs/adr/` 创建 `ADR-NNN-<kebab-slug>.md` 记录最终决策；在 `MEMORY.md` ADR 索引表追加一行
6. 更新 `MEMORY.md` 热层（迭代计划、活跃问题、技术备忘）
7. 如果决策产生了新的通用架构约束或工程标准，主动向用户提议后更新 `AGENTS.md`
8. 按需更新 `README.md`、`PRODUCT.md`

### 调试问题时

1. 复现问题
2. 定位根因，避免治标不治本
3. 修复并添加测试防止回归
4. 如果是新发现的 Bug/注意事项，记录到 `MEMORY.md` 的「活跃问题」；问题解决后从该节移除

### 文档更新速查表

> 快速判断每次需求完成后需要更新哪些文档。

| 文档 | 更新时机 | 操作 |
|:-----|:---------|:-----|
| **`需求描述.md`** | **强制 — 每次完成 Phase 或核心特性** | 更新产品路线图状态 |
| **`MEMORY.md`** 热层 | 几乎每次 | 更新迭代计划、活跃问题、技术备忘；新 ADR 只加索引行 |
| **`docs/adr/ADR-NNN-*.md`** | 新增 / 修改架构决策 | 独立文件，编号递增；禁止在 `MEMORY.md` 写详情 |
| **`docs/CHANGELOG.md`** | Phase / 核心特性完成 | 表头追加一行里程碑（时间倒序） |
| **`AGENTS.md`** | 发现新通用标准 | 日常不动；新的架构约束 / 工程标准才更新 |
| **`README.md`** | 对外说明变化 | 新功能、部署方式变更等 |
| **`PRODUCT.md`** | 用户侧功能变化 | 非技术语言 |

## 核心工作流规范 (Mandatory Operations)

### 0. 虚拟环境激活 (Virtual Environment Activation)
- **准则**：**[最高优先级]** 执行任何 Python 相关命令前，**必须**先激活项目虚拟环境，无一例外。
- **执行**：
  1. 在每次终端会话或执行 Python 脚本前，先运行：
     ```bash
     source /Users/laplace/Laplace/.venv/bin/activate
     ```
  2. 适用范围包括但不限于：`python3`、`pip`、`pytest`、`ruff`、`uvicorn`、任何 `python -m` 命令。
  3. 如果不确定当前终端是否已激活，可通过 `which python3` 验证路径是否指向 `.venv/bin/python3`。
- **目的**：避免使用系统 Python 导致依赖缺失、版本不匹配等问题。项目所有依赖均安装在 `.venv` 中。

### 1. 服务自动重载与状态校验
- **准则**：任何涉及 `server/` 目录下 Python 代码、Prompt 模板或配置文件（JSON）的修改，**必须**由 AI 主动执行服务重启，禁止要求用户手动重启。
- **执行**：
  1. 确认虚拟环境已激活（参见上方第 0 条）。
  2. 使用 `pkill -f uvicorn` 或类似命令清理旧进程。
  3. 使用 `python3 -m uvicorn server.main:app` 重新启动服务。
  4. 检查进程状态确保服务已就绪。
- **目的**：保证测试反馈的一致性，避免因缓存或未重载代码导致的"修复无效"假象。
## 架构约束与长期维护标准

为了保证项目的长期健壮性，后续开发必须严格遵守以下架构准则：

### 1. 数据后端预消化 (Pre-digestion First)
- **准则**：严禁将原始的英文枚举值（如 `saber`, `upArts`）直接传递给 LLM。
- **执行**：在 `server/main.py` 构建 Context 之前，必须通过 `CLASS_MAP` 或 `get_effect_translation` 完成中文化转换。
- **目的**：杜绝 LLM 翻译幻觉，降低 Token 消耗。

### 2. 全链路日志追踪 (Structured Logging)
- **准则**：所有的查询、解析和生成过程必须携带 `trace_id`。
- **执行**：使用 `server/logger.py` 记录结构化 JSONL 日志。每增加一个处理阶段（如新增 RAG 召回策略），必须在日志中记录其输入输出。
- **目的**：确保每个 Bug 都能通过 TraceID 回溯根因。

### 3. Schema Mirror 同步机制
- **准则**：领域知识（FuncType, BuffType）必须源自 `sync_chaldea.py` 的提取。
- **执行**：如果发现某个技能效果搜不到，优先检查 `effect_schema.json` 映射，而不是在查询逻辑中写硬编码。

### 4. LLM Contract 结构化契约
- **准则**：所有 LLM 意图解析必须通过 Pydantic 模型定义的强契约进行。
- **执行**：
  1. 优先启用 OpenAI 兼容的 `response_format/json_schema` 模式。
  2. 必须包含 Pydantic 校验环节，严禁直接使用 `json.loads()` 的原始输出进入业务逻辑。
  3. 任何路由契约的变更必须同步更新 `server/schemas.py`（`RoutingResponse` / `SkillCall`）。
  4. 新增查询维度通过新建 Skill 模块实现，Skill 的 `params_schema` 定义参数契约，无需修改全局 Schema。
- **目的**：确保 SkillExecutor 接收的数据绝对合法，消除解析幻觉和格式漂移。

### 5. Skill-Based Architecture 可扩展模式
- **准则**：所有查询逻辑必须以独立 Skill 模块实现，通过 `@register_skill` 装饰器注册到 `SKILL_REGISTRY`，禁止在任何单体函数中堆积 if-else。
- **执行**：
  1. 每个查询维度（npCharge、className、traits 等）独立为一个 `QuerySkill` 子类文件，放在 `server/skills/query/` 下。
  2. 使用 `@register_skill` 装饰器自动注册到 `SKILL_REGISTRY`。
  3. `SkillExecutor` 负责按 domain 分组 AND 合并执行，保持核心调度逻辑精简。
  4. 新增查询维度时，只需新建 Skill 文件 + 在 `server/skills/__init__.py` 追加导入，无需修改路由、执行器或 Prompt 逻辑。
  5. 每个 Skill 可选提供 Pydantic `params_schema`，校验失败自动跳过并降级。
- **目的**：控制单模块复杂度，降低未来新增礼装、关卡、素材等查询维度时的维护成本。

### 6. 知识与配置分离原则
- **准则**：稳定领域知识与可运营配置必须物理隔离。
- **执行**：
  1. `server/knowledge/` — 存放 `sync_chaldea.py` 从 Chaldea Dart 源码提取的领域知识，**严禁手工编辑**。
     - 主要是 build-time 消费（由 `data_loader.py` 生成 Materialized View）
     - 允许 runtime 读取，但仅限「查询输入映射」场景（如中文→英文效果名反查）
     - 无代码消费的纯参考文件应移到 `docs/reference/`
  2. `server/config/` — 存放可运营配置（昵称、术语映射、展示规则、Prompt 片段），支持热更新。
  3. 严禁在 `main.py`、`prompts.py` 中硬编码翻译字典（如 `CLASS_MAP`），必须从 `config/` 加载。
  4. **烘焙 vs 查表判定**：筛选字段烘焙到 MV，映射翻译 runtime 查表。详见 ADR-019。
  5. **Effect Schema Overlay 机制（绝对纪律）**：
     - `server/knowledge/effect_schema.json` 由 `sync_chaldea.py` **自动生成**，每次同步会整体覆盖，**严禁在其中手工添加任何内容**。
     - 所有手工业务扩展（虚拟复合效果如 `damageBoost`/`damageShield`、翻译修正、自定义效果分组）**必须**放入 `server/config/effect_overrides.json`。
     - `data_loader.py` 的 `merge_effect_overlay()` 函数负责在 runtime 将 overlay 合并到 schema 之上。**同名效果以 overlay 为准（覆盖）**，新效果追加到末尾。
     - 所有读取 `effect_schema.json` 的位置（`main.py`、`prompts.py`、Skill 模块等）**必须**经过 `merge_effect_overlay()` 合并后再使用，禁止直接使用原始 schema 数据。
     - 新增虚拟复合效果时，只需编辑 `server/config/effect_overrides.json`，无需修改任何代码。
- **目的**：知识更新与配置维护解耦，确保 `sync_chaldea.py` 重新同步时不会覆盖手工业务扩展。

### 7. Chaldea 依赖边界
- **准则**：`chaldea-center/chaldea` 不是 runtime 强依赖，仅 `sync_chaldea.py` 更新领域知识时需要。
- **执行**：
  1. 普通运行只依赖已生成的 `server/knowledge/*.json` 与 `server/data/servants_db.json`。
  2. 重新同步 Schema Mirror 时，从 https://github.com/chaldea-center/chaldea.git 拉取源码。
  3. 支持通过 `CHALDEA_SRC_PATH` 环境变量指定源码路径，默认 `chaldea-center/chaldea`。
  4. README 必须明确说明依赖边界，避免新人误解。
- **目的**：降低部署门槛，明确开发环境与运行环境的依赖差异。

### 8. 异步日志非阻塞
- **准则**：高并发场景下，日志写入不得阻塞 FastAPI Event Loop。
- **执行**：
  1. 使用 FastAPI `BackgroundTasks` 将 `log_chat_trace` 加入后台任务队列。
  2. API 路由完成业务逻辑后立即返回响应，日志异步写入。
  3. 禁止在 `async def` 路由中直接调用同步 `FileHandler`。
- **目的**：避免磁盘 I/O 阻塞导致的服务卡顿，提升高并发响应性能。

### 9. 前端零技术术语 (User-Facing Sanitization)
- **准则**：**[绝对纪律]** 用户在前端页面上能看到的任何文本，包括 SSE thinking steps、错误提示、卡片数据、LLM 生成回复，**严禁**出现面向开发者的技术术语。
- **执行**：
  1. **SSE 事件预消化**：后端向前端推送的所有 SSE 事件，必须使用中文用户语言，禁止暴露原始的 `skill_name`（如 `search_by_effect`）、英文参数名（如 `subStatePositive`）、内部函数名等。使用 `_describe_filters()` 等翻译函数在推送前完成中文化。
  2. **LLM Context 全中文化**：传给 LLM 的 `context_data` 中所有 JSON key 和 value 必须全部使用中文。英文 key（如 `skillEffects`、`top_results_details`）会被 LLM 当作词汇引用到回复中，导致技术术语泄露。禁止包含原始英文 Skill 名或参数值。
  5. **业务语义优先，禁止系统语义**：LLM 的回复必须始终使用业务语义（玩家自然语言），禁止使用系统语义（开发者视角的实现细节）。例如：「这里列举其中 5 位代表」✅ vs 「第6位未在JSON中呈现」❌。Generation Prompt 必须包含对应强约束。
  3. **前端映射兜底**：前端 `SKILL_DISPLAY_NAMES` 等映射表必须覆盖所有已注册的 Skill。新增 Skill 时必须同步更新前端映射，但后端预消化是第一道防线，前端映射仅作兜底。
  4. **新增 Skill 检查清单**：每次新增 Skill 模块时，必须检查以下位置是否需要同步更新：
     - `server/main.py` → `_describe_filters()` 新增中文描述分支
     - `demo/app.js` → `SKILL_DISPLAY_NAMES` 新增中文映射
- **目的**：确保用户体验始终是面向玩家的自然语言，杜绝技术实现细节泄露到用户界面。

### 10. LLM API 网关反代 (Cloudflare Bypass)
- **准则**：当 LLM API 提供商使用 Cloudflare 防护导致服务器直接请求返回 403 时，通过 Nginx 反向代理注入浏览器 User-Agent 绕过 Bot 检测，**禁止在应用代码中实现绕过逻辑**（如 FlareSolverr）。
- **执行**：
  1. 在服务器 Nginx 配置中添加 `/llm-proxy/` location block，代理到上游 API 并设置浏览器 User-Agent。
  2. 该 location 必须设置 `allow 127.0.0.1; allow 172.17.0.0/16; deny all;` 限制仅本机和 Docker 容器访问。
  3. `.env` 中的 `LLM_BASE_URL` 指向本地 Nginx 反代地址（如 `http://172.17.0.1/llm-proxy/v1`），而非直连上游 API。
  4. 80 端口和 443 端口的 server block 中均需配置 `/llm-proxy/`，因为 Docker 容器通过 HTTP 访问宿主机 Nginx。
  5. `server/llm_client.py` 保持纯净的 HTTP 客户端逻辑，不包含任何 Cloudflare 绕过代码。
- **目的**：将网络层问题隔离在基础设施层（Nginx）解决，保持应用代码简洁，避免引入不稳定的第三方绕过依赖。

## 禁止事项

- ❌ 未经确认删除文件或数据
- ❌ 引入未经审查的第三方依赖
- ❌ 修改与当前任务无关的代码
- ❌ 忽略错误处理
- ❌ 提交包含敏感信息（密钥、密码等）的代码
