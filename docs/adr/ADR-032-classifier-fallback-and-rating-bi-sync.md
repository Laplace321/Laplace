# ADR-032: 分类器 FALLBACK 链路与评分 BI 同步双修复

**状态**: 已实施
**日期**: 2026-06-12
**决策者**: Laplace
**关联 ADR**: ADR-024（两阶段路由）、ADR-026（多轮对话状态机）、ADR-030（BI SQLite 索引）

---

## 一、背景

trace `2191f1ce` 暴露两个独立但同源的问题：

### 问题 1：「你好」走错链路

```
User: "你好"
Stage 0 → pipeline=C, confidence=0.5
Stage 1 (guide BM25) → 命中 0 条
Reply: "攻略库未找到相关内容..."
```

根因：`ClassifierResponse.pipeline = Literal["A","B","C"]` 强制三选一，对纯问候 / 超出范围的输入没有出口；`after_classify` 的兜底逻辑仅覆盖 A 链路低置信度，B/C 没有，「你好」被随机扔到 C 链路就直接失败。

### 问题 2：差评未进统计面板

同 trace_id 用户提交「bad」评分后，统计面板仍显示 0 条差评。`/api/rate` 只调用 `log_trace_event` 写 JSONL，从不调用 `bi_index.upsert_turn` 刷新 SQLite，导致 `turn_summary.rating` 永远为 NULL。

---

## 二、决策

### 2.1 问题 1：扩展 Classifier Schema 增加 FALLBACK 链路（方案 A）

将 `pipeline` 枚举从 `Literal["A","B","C"]` 扩展到 `Literal["A","B","C","FALLBACK"]`，并新增 `fallback_code: Literal["greeting","out_of_scope"] | None`。Classifier 主动识别问候 / 超出范围，路由到已存在的 `template_fallback_node` 输出预置文案。

**对比候选方案**：

| 方案 | 描述 | 选择 |
|:-----|:-----|:-----|
| A. Schema 扩展 FALLBACK | 显式新链路，prompt 教 LLM 识别 | ✅ 选 |
| B. confidence 阈值兜底 | B/C 低置信度落到 agent_fallback | ❌ |

选 A 的关键理由：

1. **零 LLM 调用首字延迟**：模板回复 < 100ms，B 方案需要 agent fallback 多走一次 LLM
2. **Token 成本省 5-15K**：FALLBACK 直接出预置模板
3. **覆盖纯问候 + 纯无关**：B 方案对「明天天气」依然会跑一次完整 agent
4. **历史共识一致**：与"Schema 显式扩展优于阈值兜底"的工程经验对齐

### 2.2 双重防御防 LLM 误判

LLM 把「梅林是谁」「剑阶推荐」误判为 FALLBACK 会导致用户体验崩溃。采用「prompt 教学 + 后置关键词校验」双重防御：

1. **Prompt 严格判定（规则 8）**：列出职阶 / 专有名词 / 效果 / 数值条件等信号词，强调"任一命中必须走 A/B/C"，并提供反例「梅林是谁 → A」
2. **classify_node 后置校验**：`_FGO_SIGNAL_KEYWORDS`（30+ 关键词）+ `_has_fgo_query_signal()` 子串匹配。LLM 输出 FALLBACK 时，含任一信号则**强制改回 A**，并清空 fallback_code

权衡：「推荐」也在词表中，所以「推荐充电器」会被改回 A，再由 Stage 1 的 `out_of_scope` 兜底。这是有意为之 —— 宁可让 A 链路兜底（多消耗 1 次 LLM 调用），也不放过潜在的 FGO 查询。

### 2.3 问题 2：rate API 同步刷新 BI 索引

`/api/rate` 在 `await log_trace_event` 之后追加 `await asyncio.to_thread(upsert_turn, trace_id)`，把同步 SQLite 写包装到线程池避免阻塞 FastAPI Event Loop。`upsert_turn` 内部 SQL 已用 `COALESCE(excluded.rating, turn_summary.rating)` 实现增量更新，新评分覆盖旧值。

---

## 三、实施

### 3.1 改动清单

| 文件 | 改动 |
|:-----|:-----|
| `server/schemas.py` | `ClassifierResponse.pipeline` 枚举增加 FALLBACK；新增 `fallback_code` 字段 |
| `server/prompts.py` | `build_classifier_prompt` 增加链路 FALLBACK 段落 + 规则 8 严格判定 + 7 条 FALLBACK 示例 + 1 条反例「梅林是谁 → A」 |
| `server/nodes/classify.py` | 新增 `_FGO_SIGNAL_KEYWORDS` 关键词表 + `_has_fgo_query_signal()`；`classify_node` 解析 fallback_code 并后置防误判（含信号强制改 A）；trace event 注入 fallback_code |
| `server/edges.py` | `after_classify` 增加 FALLBACK 分支：构造 `routing_result.fallback` 并路由到 `template_fallback` 节点 |
| `server/main.py` | `/api/rate` 在 `log_trace_event` 后增加 `await asyncio.to_thread(upsert_turn, trace_id)` |
| `tests/test_classifier.py` | 放宽 prompt 长度上限至 6000；新增 FALLBACK schema roundtrip / parse / 后置防误判 / 关键词覆盖共 11 个用例 |
| `tests/test_edges_dispatch.py` | 新增 FALLBACK 路由 3 个用例 |
| `tests/test_admin_routes.py` | 新增 `/api/rate` 同步 BI 索引 3 个用例 |
| `tests/test_schemas.py` | `test_pipeline_enum_values` 修正为 `{A,B,C,FALLBACK}`；新增 `test_fallback_code_enum_values` |

### 3.2 端到端验证

| 输入 | classified_pipeline | fallback_code | reply 出处 |
|:-----|:-----|:-----|:-----|
| 你好 | FALLBACK | greeting | template `GREETING` |
| 你能做什么 | FALLBACK | greeting | template `GREETING` |
| 推荐一个充电器 | A（被后置防御改回）→ Stage 1 out_of_scope | — | template `OUT_OF_SCOPE` |
| 梅林是谁（反向） | A | — | lookup_servant 命中 1 条 |

评分链路：trace `2d347d40` 提交 `bad` → SQLite `turn_summary.rating='bad'` ✅

### 3.3 历史数据回填

执行 `bi_index.reindex_from_jsonl()`，扫描 21215 行 / 索引 2209 traces，rating 列填充 5 条历史评分（bad=3, good=1, ok=1）。

---

## 四、影响

### 4.1 Prompt 长度

Stage 0 prompt 从 2.5k → 4.2k 字符（无 prev_summary）/ 5.7k（有 prev_summary）。仍远小于 Stage 1 的 30k+，可接受。测试阈值 `< 6000` 留有余量。

### 4.2 兼容性

`fallback_code` 在 `ClassifierResponse` 中默认 `None`，旧版 LLM 不输出该字段不会校验失败；非 FALLBACK 路径主动清空 `state.extras["fallback_code"]`，保证 BI trace 字段语义干净。

### 4.3 BI 维度扩展

`classifier_output` trace event 增加 `fallback_code` 字段，可在统计面板按 greeting / out_of_scope 分桶分析无效流量比例。

---

## 五、未来工作

- 监控 `_has_fgo_query_signal()` 误改 A 的频率，若过高考虑收紧关键词（如把「推荐」从词表中移除）
- 在统计面板新增「FALLBACK 子分布」饼图（greeting / out_of_scope）
- 评估扩展 `fallback_code` 到 `unsupported`（已有 FALLBACK_TEMPLATES 但 classifier 暂不输出该值）
