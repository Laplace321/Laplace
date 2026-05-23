# ADR-022: A→B→C 三级知识问答链路架构

## 状态

Accepted

## 上下文

Trace 8589d27a 暴露了系统在非结构化知识问答场景下的幻觉问题：用户询问"最近有什么活动"、"戴冠战怎么打"等问题时，LLM 完全依赖自身训练数据生成回复，缺乏实时数据支撑，导致信息过时或编造。

当前系统仅有链路 A（Skill 精确查询），覆盖从者/礼装的结构化数据查询。对于活动历史、关卡攻略、配队推荐等非结构化知识，需要新的检索管道。

## 决策

采用 A→B→C 三级串行知识问答架构：

### 链路 A：Skill 精确查询（现有）
- **数据源**：`servants_db.json` + `ce_db.json`（预消化 Materialized View）
- **检索方式**：Pydantic 契约 → SkillExecutor 精确匹配
- **适用场景**：从者筛选、礼装查询、效果搜索等结构化查询
- **校验**：Schema 强校验，无幻觉风险

### 链路 B：Atlas 知识问答（新增）
- **数据源**：Atlas CN API（`nice_event.json`, `nice_war.json`, `nice_gacha.json`, `nice_item.json`）
- **检索方式**：倒排索引（name/tag/time/servant 关联）→ 结构化字段匹配
- **适用场景**：活动时间、卡池历史、主线关卡、素材掉落等事实性查询
- **校验**：轻量级事实验证（NER + 索引查表，70% 阈值）
- **加载策略**：懒加载单例，首次查询时触发数据拉取和索引构建

### 链路 C：攻略知识问答（新增）
- **数据源**：`server/data/guides/*.md`（YAML frontmatter + Markdown）
- **检索方式**：文档级 BM25 检索（`rank_bm25.BM25Okapi`）+ 全文传入 + 向量语义兜底（预留）
- **适用场景**：关卡攻略、配队推荐、强度评价等主观/经验性内容
- **校验**：来源标注（自动追加文档来源）
- **检索粒度**：文档级评分（同一文档 chunk 分数取 max），返回命中文档全文
- **演进路线**：当前方案 D（文档级 BM25 + 全文传入）→ 文档 >20 篇或单篇 >8k token 时升级方案 C（向量语义检索）。详见 `docs/architecture-discussions/guide-retrieval-strategy-evolution.md`

### 路由机制
- LLM 路由 Prompt 新增规则 20/21，识别 atlas/guide domain 关键词
- `RoutingResponse` 新增 `target_pipeline: Literal["A", "B", "C"] | None` 字段
- B+C 链路统一走 LLM 路由，Preset 仅用于 A 链路（Skill 参数模板+B1合并）

### 降级策略
- 链路 B 无结果 → 明确告知 + 建议更具体关键词
- 链路 C 无结果 → 明确告知 + 建议更具体关键词
- 链路 B/C 异常 → 静默降级到 Agent 兜底

## 后果

### 正面
- 消除非结构化知识问答的幻觉风险
- Atlas CN 数据提供实时活动/卡池/主线信息
- 攻略文档支持运营团队自主维护，无需代码变更
- Preset 直连避免 LLM 路由开销（~200ms → ~50ms）

### 负面
- **启动时间**：Atlas 索引首次构建需 3-5 秒（懒加载缓解，不影响服务可用性）
- **Token 成本**：链路 B/C 各增加 1 次 LLM generation 调用（~500-1000 tokens）
- **维护复杂度**：新增 3 个模块（`atlas_index.py`, `guide_retriever.py`, pipeline 分发逻辑）
- **依赖**：新增 `rank_bm25`（~50KB，纯 Python）和 `pyyaml`

### 风险缓解
- Atlas 索引懒加载，服务启动不被阻塞
- BM25 对小语料友好，无需向量数据库基础设施
- 事实校验阈值可调（当前 70%），误拦截时可降低
- 攻略文档 YAML frontmatter 标准化，便于批量导入

## 相关文件

- `server/atlas_index.py` — Atlas 倒排索引 runtime 查询接口
- `server/guide_retriever.py` — 攻略文档 BM25 检索引擎
- `server/data_loader.py` — Atlas CN 数据拉取与索引构建
- `server/pipeline.py` — 链路分发逻辑（`_handle_atlas_pipeline`, `_handle_guide_pipeline`）
- `server/prompts.py` — 路由规则 20/21
- `server/schemas.py` — `RoutingResponse.target_pipeline`
- `server/skills/presets.py` — A 链路 Preset（Skill 参数模板）
- `docs/architecture-discussions/guide-retrieval-strategy-evolution.md` — 链路 C 检索策略演进讨论
- `server/data/guides/` — 攻略文档目录
