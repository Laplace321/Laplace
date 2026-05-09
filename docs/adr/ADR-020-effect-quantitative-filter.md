# ADR-020: 效果量化筛选（技能效果数值 + 目标类型过滤）

## 状态
已采纳 (2025-07-09)

## 背景
用户查询"给队友加红魔放超过50%的从者"时，系统无法支持——因为 `skillDetails` 只存了效果名和目标类型，缺少数值信息。详见 `docs/architecture-discussions/effect-quantitative-query.md`。

## 决策

### 1. 数据层：只存满级数值（方案 A）
- `skillDetails[].effects[]` 新增 `valueMax`（万分比）、`turn`、`count` 三个字段
- `svals` 原始数据可能是 list（10 级）或 dict（已 digest），提取时做兼容处理
- 宝具效果量化（OC 1~5）复杂度较高，推迟到 Phase 8

### 2. 执行层：`_match_effect` 扩展三维过滤
- 新增 `min_value` / `max_value` 参数（万分比单位）
- 与 `target_type` 形成 effect + target + value 三维 AND 过滤

### 3. 百分比转换层：Skill filter 内部
- LLM 传百分比（50 = 50%），Skill `filter()` 内部 ×100 转万分比（5000）
- 转换逻辑集中在 `search_by_effect` 和 `search_by_skill_effect` 的 filter 方法中

### 4. 路由 Prompt
- 新增规则 10：说明 `targetType` 和 `minValue` 的使用场景
- 新增 2 个 few-shot 示例引导 LLM 正确输出

## 影响
- 数据库体积略增（每个效果多 3 个 int 字段）
- Token 消耗不变（新字段不进入 LLM context）
- 向后兼容：`minValue`/`maxValue` 为可选参数，不传时行为与之前一致
