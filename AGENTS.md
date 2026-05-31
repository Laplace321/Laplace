# Agents — 操作指南与全局约束

## 核心原则

0. **用户称呼**: 始终称呼用户为 **Laplace**，这是最基本的上下文验证锚点——如果 Agent 未使用此称呼，说明会话启动流程未正确执行
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

2. **[绝对纪律] 分支策略与合并流程（Git Flow）**：
   > 项目采用 `develop` 集成分支模式：所有功能/修复分支合并到 `develop`，经用户确认后再合并到 `main` 进行线上发布。
   
   - **分支层级**：
     ```
     feat/<slug> ──┐
     fix/<slug>  ──┤── merge → develop（集成验证）── 用户授权 → main（线上发布）
     refactor/<slug>┘
     ```
   - **分支命名规范**：`feat/<slug>`（新功能）、`fix/<slug>`（Bug 修复）、`refactor/<slug>`（重构）、`chore/<slug>`（工程改进）
   - **工作流程**：
     1. 从 `develop` 创建特性分支：`git checkout develop && git checkout -b feat/xxx`
     2. 在特性分支上开发，通过本地三步验证后 commit + push
     3. 合并到 `develop`：`git checkout develop && git merge feat/xxx && git push origin develop`
     4. **禁止自行合并到 main**：`develop → main` 的合并必须得到用户明确授权，由用户决定发布时机
   - **禁止直接在 main 或 develop 分支上开发**：所有代码变更必须在特性分支上进行
   - **例外情况**：仅纯文档更新（如 CHANGELOG、README 错别字修正）可直接在 develop 上提交，但仍建议走分支流程
   - **违规后果**：直接在 main 上开发会导致未验证代码污染主线，CI 失败影响所有人，且无法安全回滚
   - **AI 执行要求**：接到开发任务时，AI 必须主动从 `develop` 创建新分支再开始编码，不得等待用户提醒。如果当前已在 main/develop 分支且有未提交变更，必须先 stash 或创建分支后再操作
   - **develop 分支初始化**：如果 `develop` 分支尚未创建，从 `main` 创建：`git checkout main && git checkout -b develop && git push -u origin develop`

3. **Push 前必须通过三步本地验证**：
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

4. **依赖变更纪律**：
> **[绝对纪律]** 新增第三方依赖时，**必须**同步更新 `server/requirements.txt`（Dockerfile 和 CI 的唯一依赖源）。
> - **严禁**仅修改 `pyproject.toml` 的 dependencies（该文件已废弃作为运行时依赖源）
> - 本地验证：修改 `server/requirements.txt` 后，执行 `pip install -r server/requirements.txt` 再运行 `python -c "from server.<module> import <Class>"` 确认无 ImportError
> - CI 会自动校验 `server/` 下所有 import 的第三方库是否均在 `server/requirements.txt` 中声明，未声明则 CI 失败

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
5. 如果完成了 Phase 或核心特性，在 `docs/CHANGELOG.md` 追加里程碑，并按 §14 SOP 同步更新 `demo/changelog-data.json`（面向用户的更新日志）
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
| **`demo/changelog-data.json`** | **强制 — 与 CHANGELOG.md 同步** | 按 §14 SOP 追加版本条目（面向用户的更新说明） |
| **`AGENTS.md`** | 发现新通用标准 | 日常不动；新的架构约束 / 工程标准才更新 |
| **`README.md`** | 对外说明变化 | 新功能、部署方式变更等 |
| **`PRODUCT.md`** | 用户侧功能变化 | 非技术语言 |
| **`docs/architecture.html`** / **`docs/architecture.json`** | 架构 / 流程变更 | 同步更新架构图和结构化描述 |

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
- **准则**：**[绝对纪律]** 任何涉及 `server/` 目录下 Python 代码、Prompt 模板或配置文件（JSON）的修改，以及任何本地 Bug 修复后需要重启服务才能生效验证的场景，**必须由 AI 主动执行服务重启和验证**，禁止要求用户手动重启或手动验证。
- **执行**：
  1. 确认虚拟环境已激活（参见上方第 0 条）。
  2. 使用 `pkill -9 -f uvicorn` 强制清理旧进程（必须用 `-9` SIGKILL，避免优雅关闭超时导致端口未释放）。
  3. 等待 2 秒后确认端口已释放：`lsof -ti:8000`。如果仍有输出，再次 `lsof -ti:8000 | xargs kill -9` 并等待 1 秒。**严禁在端口未释放时启动新进程**，否则新进程绑定失败但健康检查可能误连旧进程，导致"修复无效"假象。
  4. 清除 Python 字节码缓存：`find server/ -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null`。
  5. 使用后台方式启动服务：`nohup python3 -m uvicorn server.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 &`
  6. 等待 3 秒后执行健康检查：`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health`，确认返回 200。
  7. 如果健康检查失败，查看 `/tmp/uvicorn.log` 排查错误。**特别注意 `address already in use` 错误**，这表示旧进程未完全退出，需回到步骤 2 重新清理。
- **适用场景**：
  - 修改了 `server/` 下的 Python 代码、Prompt、配置文件
  - 修复了 Bug 且修复效果需要服务重启后才能验证
  - 新增了数据构建逻辑（如 `data_loader.py`）并需要重新生成数据
  - 任何"改了代码但还没看到效果"的情况
- **目的**：保证测试反馈的一致性，避免因缓存或未重载代码导致的"修复无效"假象；确保 AI 完成端到端的修复-验证闭环，而非把验证工作推给用户。

### 2. 依赖完整性校验 (Dependency Completeness Check)
- **准则**：**[绝对纪律]** 每次新增 import 第三方库后，必须确认该库已在 `server/requirements.txt` 中声明。CI 会自动校验，未声明则构建失败。
- **执行**：
  1. 新增第三方 import 后，立即检查 `server/requirements.txt` 是否包含对应包名。
  2. 注意包名与 import 名的映射差异（如 `import yaml` → 包名 `pyyaml`，`from rank_bm25` → 包名 `rank-bm25`）。
  3. 快速审查命令：`grep -rh "^import\|^from" server/ | sort -u` 查看所有顶层 import。
  4. `pyproject.toml` 的 `[project] dependencies` 已废弃作为运行时依赖源，**严禁**仅在该处添加依赖。
- **目的**：杜绝本地开发环境碰巧安装了某个包导致"本地能跑但 CI/Docker 报错"的依赖幽灵问题。
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

### 10. 部署数据一致性 (Deploy Data Consistency)
- **准则**：代码变更可能影响 `servants_db.json` 的构建输出（如新增特性合并逻辑），部署时必须确保数据与代码版本一致。
- **执行**：
  1. `Dockerfile` 通过 `BUILD_VERSION` ARG 在构建时写入 `/app/.build_version` 版本戳。
  2. `docker-entrypoint.sh` 启动时对比版本戳与 `server/data/.data_build_version`，不一致则自动重建数据库。
  3. 构建镜像时**必须**传入 `--build-arg BUILD_VERSION=$(git rev-parse --short HEAD)`，否则版本戳为 `unknown`，每次启动都会触发重建。
  4. `REFRESH_DATA_ON_START=1` 仍可作为手动强制刷新的手段。
- **目的**：杜绝「代码更新但数据陈旧」导致的线上功能异常（如 traitAdd 合并逻辑上线后旧数据缺少兽科特性）。

### 11. 线上部署操作规范 (Production Deploy Discipline)
- **准则**：**[绝对纪律]** 部署到线上时，必须严格遵循以下端口映射和健康检查流程，禁止凭记忆执行。
- **执行**：
  1. **端口映射强制规则**：nginx 反代到 `127.0.0.1:8000`，Docker 容器端口映射**必须**使用 `-p 8000:8000`。**严禁**使用 `-p 8080:8000` 或任何非 8000 的 host 端口，否则 nginx 将无法连接到后端导致 502。
  2. **标准 docker run 命令**（每次部署必须参照，禁止凭记忆拼写）：
     ```bash
     docker run -d \
       --name laplace \
       --restart unless-stopped \
       -p 8000:8000 \
       -v /opt/laplace/server/logs:/app/server/logs \
       -v /opt/laplace/server/data:/app/server/data \
       --env-file /opt/laplace/.env \
       laplace:latest
     ```
  3. **部署后必须健康检查**：容器启动后，**必须**执行以下命令验证服务可达，确认 HTTP 200 后才可视为部署成功：
     ```bash
     curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health
     ```
  4. **完整部署流程（强制顺序）**：
     ```
     git pull → docker build --build-arg BUILD_VERSION=$(git rev-parse --short HEAD) -t laplace:latest .
     → docker stop laplace → docker rm laplace → docker run（见上方标准命令）
     → 健康检查重试（见下方） → 确认 200
     ```
  5. **健康检查重试机制**：容器启动后可能需要重建数据（从 Atlas API 拉取从者/礼装数据），启动时间 5s~60s 不等。**必须**使用重试而非固定等待：
     ```bash
     for i in $(seq 1 12); do
       STATUS=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health)
       [ "$STATUS" = "200" ] && echo "✅ Health check passed" && break
       echo "⏳ Attempt $i: status=$STATUS, waiting 5s..."
       sleep 5
     done
     ```
     最多重试 12 次（60s）。如果 60s 后仍未 200，立即 `docker logs laplace` 查看错误，禁止无视。
- **目的**：杜绝因端口映射错误、容器未启动等低级失误导致线上 502 不可用。

### 12. 前端静态资源部署纪律 (Frontend Asset Deploy Discipline)
- **准则**：**[绝对纪律]** 修改前端静态文件（CSS/JS）后部署到线上时，必须同步更新 HTML 中的缓存版本号，否则浏览器将继续加载旧版本缓存，导致修改不生效。
- **执行**：
  1. **缓存版本号强制递增**：`demo/index.html` 中通过 `?v=N` 查询参数控制浏览器缓存（如 `style.css?v=10`、`app.js?v=10`）。每次修改 `demo/style.css` 或 `demo/app.js` 后，**必须**在同一次提交中递增对应的 `?v=N` 版本号。
  2. **本地 index.html 污染防护**：`demo/index.html` 中的 Google Fonts `<link>` 可能被本地开发环境修改（如替换为本地字体路径）。提交前**必须**先执行 `git checkout origin/main -- demo/index.html` 回滚到远程版本，再修改版本号，避免将本地字体配置推送到线上。
  3. **禁止直接修改线上容器文件**：严禁使用 `docker exec sed` 或 `ssh sed` 直接修改运行中容器内的文件。所有前端变更必须通过 git commit → docker build → docker run 标准流程部署，确保镜像与代码一致。
  4. **外部 CDN 国内镜像规则**：前端引用的外部 CDN 资源（如 Google Fonts）**必须**使用国内可访问的镜像源，禁止直接使用被墙或高延迟的原始域名。当前项目标准镜像：
     - `fonts.googleapis.com` → `fonts.loli.net`
     - `fonts.gstatic.com` → `gstatic.loli.net`
     - `<link>` 标签需添加 `crossorigin` 属性以配合镜像源的 CORS 策略
     - 新增外部 CDN 依赖时，必须先验证国内可达性（`curl --connect-timeout 5`），不可达则必须替换为镜像或本地化。
  5. **前端部署检查清单**（每次涉及 `demo/` 目录的部署必须逐项确认）：
     ```
     ✅ demo/index.html 中 style.css?v=N 版本号已递增
     ✅ demo/index.html 中 app.js?v=N 版本号已递增（如 JS 有改动）
     ✅ demo/index.html 不包含本地字体修改（对比 origin/main）
     ✅ 外部 CDN 使用国内镜像源（非 googleapis.com 等被墙域名）
     ✅ 通过标准 docker build → docker run 流程部署（非容器内直接修改）
     ✅ 部署后浏览器硬刷新（Ctrl+Shift+R）验证新版本生效
     ```
- **目的**：杜绝因浏览器缓存导致的「代码已更新但线上不生效」问题、因本地开发配置污染导致线上字体加载失败、以及因外部 CDN 在国内不可达导致的资源加载失败。

### 13. 架构文档同步纪律 (Architecture Documentation Sync)
- **准则**：每次迭代涉及系统架构或业务流转过程的调整，**必须**同步更新架构文档。
- **执行**：
  1. `docs/architecture.html`（面向开发者）：更新对应的架构图、流程描述、模块说明。
  2. `docs/architecture.json`（面向 Agent）：更新对应的模块定义、流程步骤、Skill 列表。
  3. 触发更新的变更类型包括但不限于：
     - 新增/删除/重命名 Skill 模块
     - 请求处理流程变更（如新增路由路径、降级策略调整）
     - 数据层结构变更（如新增知识库文件、配置文件）
     - LLM 调度策略变更（如新增 Provider、降级逻辑调整）
     - API 端点变更
  4. 两个文件的 `version` / 版本信息必须与当前发布版本保持一致。
- **目的**：确保开发者和 AI Agent 始终能获取到最新的系统架构信息，降低上下文理解成本。

### 14. 更新日志发版流程 (Changelog Release SOP)
- **准则**：当用户在会话中说"生成本次更新日志"时，AI 必须执行以下标准流程生成面向用户的更新记录。
- **触发条件**：用户明确表示"生成更新日志"/"发版"/"生成 changelog" 等意图。
- **执行流程**：
  1. **确认版本范围**：询问用户本次版本号（如 v0.8），或从 `demo/changelog-data.json` 中读取上一个版本号并自动递增。
  2. **提取 commit 列表**：执行 `git log --oneline <上次版本最后commit>..HEAD`，获取自上次发版以来的所有 commit。
     - 如果 `changelog-data.json` 已有记录，通过最近一条记录的日期 + `git log --after` 确定范围。
     - 如果是首次生成，取所有 commit 或由用户指定起始点。
  3. **分类汇总**：按 Conventional Commits 前缀分类：
     - `feat` → features（功能更新）
     - `fix` → fixes（问题修复）
     - `docs`/`style`/`refactor`/`test`/`chore` → others（其他）
     - 忽略纯工程性质的 commit（如 `chore(deploy): bump version`）
  4. **读取 CHANGELOG.md**：从 `docs/CHANGELOG.md` 最新一行获取概括性标题，作为本次版本的 `title` 字段。
  5. **LLM 翻译润色**：将分类后的 commit messages 发送给 LLM，要求：
     - 将英文/技术语言翻译为面向玩家的中文自然语言
     - 每条生成 `title`（10字以内简称）+ `desc`（一句话描述功能价值）
     - 合并同一功能的多个 commit 为一条记录
     - 过滤掉用户不关心的纯工程变更
  6. **生成 JSON 并追加**：将结果格式化为 JSON 对象，插入到 `demo/changelog-data.json` 的 `versions` 数组头部（最新版本在前）。
  7. **展示给用户确认**：将生成的 JSON 内容展示给用户，确认无误后写入文件。
  8. **提交**：`git add demo/changelog-data.json && git commit -m "docs(changelog): add vX.Y release notes" && git push`- **LLM 润色 Prompt 模板**：
  ```
  你是一个产品更新日志撰写者。请将以下 git commit messages 翻译润色为面向 FGO 玩家的中文更新说明。
  要求：
  1. 使用玩家能理解的自然语言，不出现任何技术术语
  2. 每条生成 title（10字以内）和 desc（一句话说明功能价值）
  3. 相关联的多个 commit 合并为一条
  4. 过滤纯工程/部署类变更（用户不关心）
  
  Commit 列表：
  {commits}
  
  输出 JSON 格式（必须使用嵌套 sections 结构，与 changelog.js 的 renderVersionContent 保持一致）：
  {"sections": {"features": [...], "fixes": [...], "others": [...]}}
  ```
- **JSON 数据结构契约（绝对纪律）**：
  - `changelog-data.json` 中每个版本对象**必须**使用嵌套 `sections` 结构：`{"version", "date", "title", "sections": {"features": [], "fixes": [], "others": []}}`
  - **严禁**使用扁平结构（如 `{"version", "date", "title", "features": [], "fixes": [], "others": []}`），这会导致 `changelog.js` 无法渲染
  - 新增版本时，先读取 `changelog-data.json` 中已有版本的实际结构作为参照，确保格式完全一致
  - 写入 JSON 后，**必须**在本地浏览器打开 `demo/changelog.html` 验证最新版本渲染正确（features/fixes/others 各分区均有内容显示），确认无误后再提交- **目的**：确保更新日志生成流程标准化、可重复，杜绝手工编写 JSON 的低效和遗漏。

### 15. LLM Streaming 接口一致性 (Streaming Interface Consistency)
- **准则**：**[绝对纪律]** `chat_completion_stream()` 与 `chat_completion()` 的参数语义和行为约束需保持一致。所有 LLM 调用（流式与非流式）统一经过 `server/llm/provider.py` 调度，禁止在 pipeline 层绕过 provider 直接调用 SDK。
- **执行**：
  1. **新增/修改 Adapter** → 必须同时实现 `chat_completion()` 和 `chat_completion_stream()` 两个接口
  2. **修改 Provider 调度逻辑**（降级策略、模型选择、错误处理）→ 需同时覆盖 streaming 和非 streaming 两条路径
  3. **变更 Prompt / temperature / max_tokens 等参数** → 检查 `_build_guide_generation_prompt()` 和 `_handle_guide_pipeline()` 是否需要同步更新
  4. **升级 OpenAI SDK / DashScope SDK** → 同时验证 `chat_completion()` 和 `chat_completion_stream()` 两处兼容性
- **关键文件对照表**：
  | 非流式接口 | 流式接口 |
  |:---|:---|
  | `server/llm/base.py` → `chat_completion()` | `server/llm/base.py` → `chat_completion_stream()` |
  | `server/llm/provider.py` → `chat_completion()` | `server/llm/provider.py` → `chat_completion_stream()` |
  | `server/pipeline.py` → `_handle_guide_pipeline()` | `server/pipeline.py` → `stream_event_generator()` 链路 C 分支 |
- **目的**：确保流式与非流式路径行为一致，避免功能漂移。此约束已同步记录到记忆系统 `feedback_bc_generation_adapter_decoupling.md`。

## 禁止事项

- ❌ 未经确认删除文件或数据
- ❌ 引入未经审查的第三方依赖
- ❌ 修改与当前任务无关的代码
- ❌ 忽略错误处理
- ❌ 提交包含敏感信息（密钥、密码等）的代码
- ❌ **未经用户明确确认，禁止将任何变更部署到线上环境**（包括但不限于：执行远程服务器上的 docker build/run/restart、SSH 到线上执行部署脚本等操作。所有线上部署动作必须在用户完成本地测试并明确授权后才可执行）
