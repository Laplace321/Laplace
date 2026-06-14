# 全链路埋点与 BI 适配性改造（v0.5.1）

**日期**: 2026-06-15
**状态**: 已实施
**参与者**: Laplace
**关联 ADR**: [ADR-005](../adr/ADR-005-structured-logging.md) / [ADR-028](../adr/ADR-028-declarative-pipeline-migration.md) / [ADR-029](../adr/ADR-029-trace-contextvar.md) / [ADR-030](../adr/ADR-030-bi-sqlite-index.md)

---

## 一、问题背景

v0.5.0 自研 DAG 引擎（11 节点 + StateGraph）落地后，原 ADR-005 时代的埋点体系未同步演进。全链路体检发现 10 处盲区：

| # | 盲区 | 影响 |
|:--|:-----|:-----|
| 1 | ContextVar 缺失 | trace_id 完全靠 state 显式传，深层调用易丢 |
| 2 | 节点埋点不一致 | classify 1 个事件 vs generate 4 个，schema 各异 |
| 3 | with_trace 空置 | 装饰器已定义但无人引用 |
| 4 | 多轮对话埋点 | turn_type / session_id / minor_merge_* 散落 |
| 5 | SSE vs JSON 顺序差异 | delta vs final 不对齐 |
| 6 | BI 维度有限 | compute_log_stats 仅按 query 前 20 字符做"路径" |
| 7 | 错误埋点不规范 | 异常 try/except 散落 pipeline.py |
| 8 | 新旧 API 混用 | log_chat_trace_async vs log_trace_event 并存 |
| 9 | 日志无轮转 | query_trace.jsonl 单文件无限膨胀 |
| 10 | 告警与 trace_id 无关联 | Bark 通知无法回溯具体请求 |

## 二、改造目标

- 节点级 trace 一致性（统一 phase 命名 + input/output/error schema）
- BI 维度扩充（pipeline / turn_type / skill_name / clarification_type / error_reason）
- ContextVar 自动传播 trace_id，告别显式传递
- JSONL 仍为事实源，新增 SQLite 索引层支撑多维聚合查询
- 日志按天轮转，CLI 按 keep_days 清理
- 告警通知携带最近 trace_id，可一键回溯
- Prometheus 指标增加业务维度（pipeline / skill_name / node_name），label 基数受控

## 三、关键决策

### 3.1 埋点框架：ContextVar + @with_trace 装饰器

详见 [ADR-029](../adr/ADR-029-trace-contextvar.md)。11 节点全量改造，删旧 API。

### 3.2 BI 落地：JSONL + SQLite 双层

详见 [ADR-030](../adr/ADR-030-bi-sqlite-index.md)。JSONL 是事实源，SQLite 是 final 事件的派生索引。

### 3.3 Prometheus 维度

| 指标 | label | 基数控制 |
|:----|:----|:----|
| `laplace_pipeline_requests_total` | pipeline / turn_type / status | 6 × 3 × 4 = 72 |
| `laplace_skill_calls_total` | skill_name / domain / status | SKILL_REGISTRY ≈ 20 × 4 × 4 |
| `laplace_node_latency_seconds_bucket` | node_name / result | 11 × 3 |
| `laplace_clarifications_total` | clarification_type | 3 |

单测 `tests/test_monitor_skill_label.py` 断言 `skill_name` 集合 == `SKILL_REGISTRY.keys()`，杜绝任意字符串导致 label 基数爆炸。

### 3.4 告警关联

`alerter.Alerter` 维护 `_recent_failure_traces`（FIFO，上限 5）：
- `metrics.record_llm_call` 失败分支自动 `push_failure_trace(get_trace_id())`
- `send_alert` 在非 RECOVERY 级别时 `consume_recent_failure_traces`，把 trace_id 渲染为 `/admin/logs?trace_id=xxx` 链接拼到 message 末尾

### 3.5 日志按天轮转

- 文件名：`query_trace.YYYY-MM-DD.jsonl`（北京时间）
- legacy `query_trace.jsonl` 单文件保留为只读 fallback
- 读取 API 全部走 `_iter_log_files()`
- CLI：`python -m server.logger cleanup --keep-days 30`

## 四、任务拆解（7 Task）

| Task | 主题 | 状态 | Commit |
|:----|:----|:----|:----|
| 1 | 埋点框架基建（ContextVar + Phase + with_trace） | ✅ | 7283cb0 |
| 2 | 11 节点 @with_trace + 入口 bind_trace_id | ✅ | 72583b1 |
| 3 | PipelineState.metric_labels + 节点维度回填 | ✅ | fde839b |
| 4 | bi_index.py + SQLite + reindex CLI | ✅ | f623735 |
| 5 | Prometheus 业务维度指标 | ✅ | 2949ad9 |
| 6 | 日志按天轮转 + 告警关联 trace_id | ✅ | d450af5 |
| 7 | 测试与文档同步 | ⏳ | - |

## 五、风险与回退

| 风险 | 缓解 |
|:----|:----|
| ContextVar 在 BackgroundTasks 失效 | FastAPI 默认继承；如失效则 `copy_context()` |
| SQLite 写入失败 | upsert_turn try/except，不阻塞 JSONL 落盘 |
| SQLite 损坏 | 删 sqlite + reindex 全量重建 |
| with_trace 改造影响测试 | 节点签名不变，仅装饰器叠加，单测平滑通过 |
| 单 Task 出错 | 每个 Task 独立 commit，可单独 revert |

## 六、发布纪律

- 全程在 `feat/observability-upgrade` 分支开发
- 每个 Task 完成后跑三步验证（ruff check / format / pytest）后 commit + push
- 全部 Task 完成后合并到 `develop`，等用户授权再合 main
- 不主动合并到 main
