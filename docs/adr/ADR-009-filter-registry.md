# ADR-009: Query Executor 采用 Filter Registry 模式

- **日期**: 2026-05-06
- **状态**: 已采纳（Phase 5 P1）

## 背景
`_match_servant` 函数 170 行，圈复杂度 25+，新增查询维度时会继续膨胀。

## 决策
采用 Filter Registry / Strategy Pattern，每个过滤维度独立为函数，通过 `@register_filter` 装饰器注册，主匹配函数仅遍历注册表执行。

## 理由
控制圈复杂度 < 10，支持未来礼装、关卡、素材等查询维度的低成本扩展。
