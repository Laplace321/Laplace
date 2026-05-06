# Memory — 项目记忆

> 长期知识库，记录关键决策、已知问题和项目演进历史。
> AI 在每次会话开始时应阅读此文件以获取上下文。

## 后续迭代计划 (Next Steps)

1. **Phase 5 Batch 2 执行中**：优先处理 P0 技术债（数据入口单一化、生产访问边界），然后推进 P1 可扩展性改造（Query Executor Filter Registry、配置外置、Trace 调试闭环）。详见 `需求描述.md` Phase 5 详细规划。
2. **多语言 UI 本地化支持 (I18n)**: 按照系统本地的语言（Locale）做显示。在前端识别用户的 `navigator.language`，如果是 `zh-CN` 等中文环境，则优先展示 `aliasCN`（中文名）与中文职阶名称；如果是其他环境，则展示 `name`（英文名）与原名。
3. **更多查询维度的支持**: 增加指令卡性能、宝具 NP 回收等进阶硬核数值查询。

## 架构决策记录 (ADR)

### ADR-001: FGO NP 数据精度约定
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: FGO 数据中 NP 值以万分之一为单位存储
- **决策**: `svals[].Value = 3000` 表示 30%，精度 = Value / 100
- **理由**: 与 Atlas Academy API 和 Chaldea 源码保持一致

### ADR-002: 数据获取方式
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: 需要获取全量从者技能数据
- **决策**: 使用 Atlas Academy 的批量导出端点 `nice_servant_lang_en.json`，Python 预处理后生成静态 JSON
- **理由**: 避免前端频繁调 API，数据可离线使用

### ADR-003: AI Native 架构 — LLM 意图解析 + Query Executor
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: 从静态 Demo 升级为 AI Native 对话式产品
- **决策**: LLM 只做意图解析和结果格式化，数据查询由中间层 Query Executor 执行
- **理由**: 分离关注点，LLM 输出的结构化 JSON 指令可预测、可测试，避免 LLM 直接操作数据带来的不可靠性

### ADR-004: Schema Mirror — 知识提取而非代码复制
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: Chaldea 用 Dart 构建了 FGO 最完整的数据类型系统（165+ FuncType、200+ BuffType、40+ SkillEffect 效果分类），需要决定如何利用
- **决策**: 不直接翻译 Dart 代码为 Python，而是提取 Chaldea 的「领域知识」（效果分类体系、枚举映射、数据路径约定）生成 JSON 知识库，注入 LLM System Prompt
- **理由**:
  1. Chaldea Dart 模型高度耦合 Flutter UI（路由、翻译、渲染），直接翻译代价高
  2. 知识提取方式维护成本低，Chaldea 更新时只需重新提取枚举
  3. LLM 具备知识后可自动处理新的查询类型，不需要每种效果都写查询逻辑

### ADR-005: 全链路日志追踪 (Logging & Traceability)
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: 用户反馈 LLM 偶尔会出现胡言乱语（如“破盾”），需要手段回溯其意图解析与召回上下文的原始状态
- **决策**: 实现基于 `trace_id` 的结构化 JSONL 日志记录。请求进入时生成 UUID，贯穿 API -> 意图解析 -> 数据库查询 -> RAG 生成。日志持久化于 `server/logs/query_trace.jsonl`
- **理由**: 提高可观测性，使“黑盒”LLM 的行为可审计、可重现

### ADR-006: 数据后端预消化 (Pre-digestion)
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: LLM 在 RAG 阶段翻译英文枚举（如 `breakAvoidance`）不专业，且浪费 Token
- **决策**: 在 Python 组装 JSON 上下文时，强制将职阶、卡色、技能效果、宝具效果翻译为标准中文术语后再投喂给 LLM
- **理由**: 根除术语翻译幻觉，精简 Prompt，降低 Token 成本，提升输出专业度

### ADR-007: LLM Contract — JSON Schema + Pydantic 校验
- **日期**: 2026-05-05
- **状态**: 已采纳
- **背景**: `llm_client.py` 原先依赖手动截取 Markdown code fence 后执行 `json.loads()`，在模型漏写 JSON 或输出额外文本时不稳定
- **决策**: 首阶段意图解析优先使用 OpenAI-compatible `response_format/json_schema`；若模型网关明确不支持，则自动降级到普通文本 JSON 提取。所有 JSON 输出必须通过 `server/schemas.py` 中的 Pydantic Contract 校验后才能进入 Query Executor
- **理由**: 在 API 层和应用层双重约束 LLM 输出，减少解析幻觉和格式漂移，同时保持与现有模型回退链兼容

### ADR-008: Phase 5 架构治理优先级重排
- **日期**: 2026-05-06
- **状态**: 已采纳
- **背景**: Phase 5 原始规划未明确优先级，且缺少部分关键治理项（异步日志、配置外置、Chaldea 依赖边界）
- **决策**: 
  1. 按 P0/P1/P2 三级重排任务优先级
  2. P0: 数据入口单一化、生产访问边界（技术债 + 安全）
  3. P1: Query Executor Filter Registry、配置外置、Trace 调试闭环（可扩展性）
  4. P2: LLM Retry、前端体验、异步日志非阻塞、工程自动化（可靠性）
  5. 新增：知识提取防护（正则样本测试、翻译映射一致性校验）
- **理由**: 确保先解决最高风险的技术债和安全隐患，再推进架构优化

### ADR-009: Query Executor 采用 Filter Registry 模式
- **日期**: 2026-05-06
- **状态**: 已采纳（Phase 5 P1）
- **背景**: `_match_servant` 函数 170 行，圈复杂度 25+，新增查询维度时会继续膨胀
- **决策**: 采用 Filter Registry / Strategy Pattern，每个过滤维度独立为函数，通过 `@register_filter` 装饰器注册，主匹配函数仅遍历注册表执行
- **理由**: 控制圈复杂度 < 10，支持未来礼装、关卡、素材等查询维度的低成本扩展

### ADR-010: 知识与配置物理分离
- **日期**: 2026-05-06
- **状态**: 已采纳（Phase 5 P1）
- **背景**: `main.py` 硬编码 `CLASS_MAP`、`NP_CARD_MAP`，`prompts.py` 硬编码效果别名，与 `knowledge/` 中的知识库可能不同步
- **决策**: 
  1. `knowledge/` 存放稳定领域知识（sync_chaldea.py 生成）
  2. 新建 `config/` 存放可运营配置（昵称、术语映射、展示规则）
  3. 严禁在代码中硬编码翻译字典，必须从 config 加载
- **理由**: 知识更新与配置维护解耦，支持运营团队独立修改配置

### ADR-011: Chaldea 依赖边界明确化
- **日期**: 2026-05-06
- **状态**: 已采纳
- **背景**: 新人容易误解 `chaldea-center/chaldea` 是 runtime 强依赖
- **决策**: 
  1. 明确仅 `sync_chaldea.py` 更新领域知识时需要 Chaldea 源码
  2. 普通运行只依赖 `knowledge/*.json` 和 `servants_db.json`
  3. 支持 `CHALDEA_SRC_PATH` 环境变量指定源码路径
  4. Chaldea 源码从 https://github.com/chaldea-center/chaldea.git 拉取
- **理由**: 降低部署门槛，避免不必要的 git submodule 或 clone 操作

### ADR-012: LLM 客户端迁移至 Responses API
- **日期**: 2026-05-06
- **状态**: 已采纳
- **背景**: OpenAI 官方推荐 Responses API 替代 Chat Completions API（2025 年起）
- **决策**:
  1. 端点: `/v1/chat/completions` → `/v1/responses`
  2. 参数: `messages` → `input`, system role → `instructions`
  3. 结构化输出: `response_format` → `text.format`
  4. 响应解析: `choices[0].message.content` → `output_text`
  5. 保留 fallback 链和 Pydantic 校验逻辑不变
- **理由**: 
  1. Responses API 是 OpenAI 最新推荐接口，支持更强大的 agentic 功能
  2. 更好的缓存利用率（40-80% 提升）
  3. 更清晰的语义（instructions vs input 分离）
  4. 未来 GPT-5+ 模型将优先支持 Responses API

### ADR-013: Thinking Steps SSE 流式交互
- **日期**: 2026-05-06
- **状态**: 已采纳
- **背景**: 用户发送查询后只看到三个跳动圆点（typing indicator），3-8 秒的等待全程黑盒，体验差
- **决策**:
  1. 新增 `GET /api/chat/stream` SSE 端点，分阶段推送事件（thinking → servants → delta → done）
  2. 前端使用 `fetch` + `ReadableStream` 消费 SSE，逐阶段渲染 Thinking Steps
  3. 从者卡片在数据查询完成后立即展示（卡片先行），不等 RAG 生成
  4. 保留原有 `POST /api/chat` 端点向后兼容
  5. 提取 `_build_context()` 共享函数，供两个端点复用
- **理由**:
  1. 零额外 Token 消耗 — 只改变传输方式，不改变 LLM 调用逻辑
  2. 参考主流 AI 产品（Perplexity / ChatGPT）的 Thinking Steps 交互模式
  3. 用户感知等待时间从"3-8s 黑盒"降低为"每阶段 1-3s 有持续反馈"

## 已知问题 & 解决方案

- **macOS pip 外部管理**: `pip install` 报错 `externally-managed-environment`，需要使用 `python3 -m venv .venv` 创建虚拟环境
- **pytest 未预装**: Phase 5 起 `pytest` 已加入 `server/requirements.txt`，新环境需重新执行 `pip install -r server/requirements.txt`
- **昵称被 LLM 改写导致搜不到从者**: 如 `水 C 呆` 被改写成 `泳装阿尔托莉雅` 后，数据库无法命中。解决方案是双层防护：`prompts.py` 明确要求保留社区别名原文，`query_executor.py` 对昵称匹配做归一化（忽略空格/常见分隔符）并优先按昵称表做精确映射。

## 项目里程碑

| 日期 | 事件 | 备注 |
| :--- | :--- | :--- |
| 2026-05-05 | 项目初始化 | 创建 OpenClaw 风格的项目骨架 |
| 2026-05-05 | Demo v1 完成 | 30% NP 自充筛选器 (Python + Web) |
| 2026-05-05 | AI Native v1 | 对话式查询上线（FastAPI + LLM 意图解析 + 从者卡片） |
| 2026-05-05 | 架构升级 | 确立 Schema Mirror 策略，目标对标 Chaldea 全数据查询 |
| 2026-05-05 | Phase 2 完成 | 实现了 sync_chaldea.py 提取 5 个 Dart 文件的效果知识并与 LLM 集成 |
| 2026-05-05 | Phase 3 完成 | 实现了多语言映射、特性（Trait）匹配算法、宝具与配卡等从者深层属性过滤 |
| 2026-05-05 | Phase 4 完成 | 实现了 Two-Step RAG 架构（生成式 UI），分离了 LLM 总结文案与 UI 数据流 |
| 2026-05-05 | Phase 5 启动 | 实现了全链路日志追踪（Logging）与数据预消化（Pre-digestion），补齐了宝具特效解析 |
| 2026-05-05 | Phase 5 Batch 1 | 完成 LLM Contract、Query Executor 回归测试、Schema Mirror 回归测试与真实 LLM JSON Schema smoke test |
| 2026-05-06 | LLM API 迁移 | 从 Chat Completions API 迁移至 OpenAI Responses API（2025 推荐） |
| 2026-05-06 | Phase 5 Batch 2 - P0 | 完成数据入口单一化：extractor/np_charge_filter.py 从 191 行降至 52 行，复用 data_loader.py |
| 2026-05-06 | Thinking Steps SSE | 新增 SSE 流式端点，分阶段展示 AI 思考过程（解析→检索→生成），卡片先行渲染 |

## 技术备忘

- **LLM 意图解析链路**: 用户输入 → LLM 解析为 JSON 指令 → Query Executor 执行 → LLM 格式化结果 → 返回对话框
- **数据精度**: FGO NP 值以 1/10000 为单位存储，`Value=3000` 表示 30%
- **Atlas Academy 批量端点**: `https://api.atlasacademy.io/export/JP/nice_servant_lang_en.json`
- **Schema Mirror 知识源**: Chaldea `effect.dart` (SkillEffect 40+分类)、`func.dart` (FuncType 165+)、`buff.dart` (BuffType 200+)、`common.dart` (SvtClass 50+)
- **Chaldea 关键数据路径**: `servant.skills[] → skill.functions[] → function.svals[9].Value` (Lv.10数值)
- **效果分类体系**: 攻击(20种) / 防御(11种) / 异常(15种) / 辅助(9种) = 55+ 子分类
- **测试命令**: 默认回归测试使用 `.venv/bin/python -m pytest`；真实 LLM smoke test 使用 `RUN_LIVE_LLM_TESTS=1 .venv/bin/python -m pytest tests/test_llm_client_live.py -s`
