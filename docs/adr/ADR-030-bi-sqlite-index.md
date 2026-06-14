# ADR-030: BI 索引层（JSONL 事实源 + SQLite 索引层 + 维度扩充）

**状态**: 已实施
**日期**: 2026-06-15
**决策者**: Laplace
**关联 ADR**: ADR-005（结构化日志）、ADR-029（trace ContextVar）

---

## 一、背景

ADR-005 之后所有 trace 事件以 JSONL 形式落 `server/logs/query_trace.jsonl`，admin 后台靠 [logger.compute_log_stats](file:///Users/laplace/Laplace/server/logger.py) 全量扫描 JSONL 做聚合。在 v0.5.0 自研 DAG 引擎落地后，调研发现 BI 维度严重不足：

1. **维度有限**：`compute_log_stats` 仅按 `query` 前 20 字符做"路径"，缺 `turn_type` / `pipeline` / `skill_name` / `error_reason` / `latency_bucket`
2. **聚合性能差**：JSONL 单文件 14k+ 行，全量扫描做聚合无法支撑多维 BI 查询
3. **错误归因弱**：异常路径分散在 [server/pipeline.py](file:///Users/laplace/Laplace/server/pipeline.py) 各 try/except，error_reason 无统一收口
4. **日志无轮转**：[server/logs/query_trace.jsonl](file:///Users/laplace/Laplace/server/logs/query_trace.jsonl) 单文件无限膨胀，归档/清理无机制

## 二、决策

### 2.1 双层存储：JSONL（事实源） + SQLite（索引层）

```
┌─────────────────────────────────────────┐
│  log_trace_event(phase=*)               │
│       ↓                                 │
│  query_trace.YYYY-MM-DD.jsonl           │ ← 事实源（不可变追加）
│       ↓ (phase=='final' only)           │
│  bi_index.upsert_turn(event)            │ ← 同步 + try/except 容错
│       ↓                                 │
│  bi_index.sqlite                        │ ← 索引层（支撑 BI 查询）
│  ├── turn_summary 表                    │
│  └── 4 个索引                           │
└─────────────────────────────────────────┘
```

**SQLite 表 `turn_summary`**：

| 字段 | 类型 | 说明 |
|:----|:----|:----|
| trace_id | TEXT PK | 唯一键 |
| ts | TEXT | ISO8601 时间戳 |
| session_id | TEXT | 会话 ID |
| turn_type | TEXT | new / continuation / clarification |
| pipeline | TEXT | direct / full_a / sse / preset / confirmation / fallback |
| skill_names | TEXT | 逗号分隔已执行 skill |
| clarification_type | TEXT | multi_match / empty / refine |
| error_reason | TEXT | routing_error / stream_error / preset_stream_error / ... |
| latency_ms | INTEGER | 端到端延时 |
| total_tokens | INTEGER | LLM 总 token |
| rating | INTEGER | 用户评分 |
| model | TEXT | LLM 模型名 |
| query_hash | TEXT | query SHA1 |
| query_preview | TEXT | query 前 80 字符 |

**索引**：`(ts)` / `(pipeline, turn_type)` / `(error_reason)` / `(session_id, ts)`

### 2.2 PipelineState.metric_labels

```python
@dataclass
class PipelineState:
    ...
    metric_labels: dict[str, str | int] = field(default_factory=dict)
```

各节点在自己阶段产出后回填：
- `classify` 写 `turn_type` / `pipeline` / `has_prev_turn`
- `execute` 写 `skill_names`（dict 去重 join）
- `clarify` 写 `clarification_type`
- `generate` 写 `model` / `total_tokens`
- 异常路径回填 `error_reason`

`generate_node` final 事件携带完整 `metric_labels`，被 `bi_index.upsert_turn` 索引落库。

### 2.3 compute_log_stats 走 SQL 聚合

```python
# 旧：扫 JSONL 14k+ 行 in-memory aggregate
# 新：SQLite SQL group by + count
SELECT pipeline, turn_type, COUNT(*) FROM turn_summary
WHERE ts >= ? GROUP BY pipeline, turn_type;
```

输出 schema 兼容现有 admin 后台，前端无需变动。

### 2.4 reindex CLI

```bash
python -m server.bi_index reindex                   # 默认遍历所有日志文件
python -m server.bi_index reindex --jsonl <path>    # 指定单文件
```

支持从历史 JSONL 全量重建 SQLite，保证两层一致性。

### 2.5 日志按天轮转

- 文件名：`query_trace.YYYY-MM-DD.jsonl`（按北京时间）
- 写入路径 `_get_log_file_for_today()`，跨天自动切换
- 读取 API（`find_trace_events` / `read_traces` / `read_trace_summaries` / `_compute_log_stats_legacy`）改为遍历 `_iter_log_files()`，包含 legacy 单文件 fallback
- CLI：`python -m server.logger cleanup --keep-days 30`，启动时不自动清理（cron 触发）

## 三、实施情况（已完成）

- [server/bi_index.py](file:///Users/laplace/Laplace/server/bi_index.py)：新建模块，含 `init_db` / `upsert_turn` / `query_stats` / `reindex_from_jsonl`
- [server/logger.py](file:///Users/laplace/Laplace/server/logger.py)：`log_trace_event` 在 `phase=='final'` 时同步调 `bi_index.upsert_turn`（try/except 容错）
- [server/graph/state.py](file:///Users/laplace/Laplace/server/graph/state.py)：`PipelineState.metric_labels` 字段添加
- 各节点回填维度（classify / execute / clarify / generate）
- [server/pipeline.py](file:///Users/laplace/Laplace/server/pipeline.py) 异常路径回填 `error_reason`
- 日志按天轮转 + CLI cleanup 已落地（[ADR-029](file:///Users/laplace/Laplace/docs/adr/ADR-029-trace-contextvar.md) 配套）
- 测试：[tests/test_bi_index.py](file:///Users/laplace/Laplace/tests/test_bi_index.py)（upsert 幂等 / SQL 聚合 / reindex 一致性）、[tests/test_log_rotation.py](file:///Users/laplace/Laplace/tests/test_log_rotation.py)（按天轮转 / 跨文件读取 / cleanup）

## 四、风险与回退

- **SQLite 写入失败**：`upsert_turn` 包 try/except，仅记录 ERROR 日志，不阻塞 final 事件落 JSONL（**JSONL 始终是事实源**）
- **SQLite 损坏**：删除 `server/logs/bi_index.sqlite` 后跑 `python -m server.bi_index reindex` 全量重建
- **JSONL 与 SQLite 不一致**：定期跑 reindex 对齐；admin 后台首选 SQLite 数据，必要时 fallback 到 `_compute_log_stats_legacy` 直接扫 JSONL
- **回退路径**：`compute_log_stats` 保留 legacy 实现，删除 `bi_index.py` 不影响 JSONL 落盘

## 五、与 ADR-005 的关系

ADR-005 确立了 JSONL 作为 trace 事实源的不变性，本 ADR **不打破该不变性**：

- JSONL 仍是事实源，所有 trace 事件无条件落 JSONL
- SQLite 只是 final 事件的派生索引，可任意删除/重建
- admin 后台首选 SQLite 查询性能，但 trace_id 单点回溯仍走 JSONL（`find_trace_events`）
