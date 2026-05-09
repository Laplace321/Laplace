# ADR-020: 效果量化筛选（技能+宝具效果数值 + 目标类型过滤）

## 状态
已采纳 (2025-07-09)，修订 (2026-05-09)

## 背景
用户查询"给队友加红魔放超过50%的从者"时，系统无法支持——因为 `skillDetails` 只存了效果名和目标类型，缺少数值信息。详见 `docs/architecture-discussions/effect-quantitative-query.md`。

## 决策

### 1. 数据层
- **技能**：`skillDetails[].effects[]` 新增 `valueMax`（千分比‰，Lv10 满级）、`turn`、`count`
- **宝具**：`npDetails[].effects[]` 新增 `valueLv1`（千分比‰，OC1 Lv1 = svals[0]）、`turn`、`count`
- FGO `svals.Value` 单位是**千分比**（‰），`500 = 50%`
- `svals` 原始数据可能是 list 或 dict，提取时做兼容处理

### 2. 执行层：三维过滤（effect + target + value）
- `_match_effect()`：检查 `skillDetails`，数值字段为 `valueMax`
- `_match_np_effect()`：检查 `npDetails`，数值字段为 `valueLv1`（默认 OC1 Lv1）
- `_check_effect()`：统一调度 `hit_skill` + `hit_np`，按 source 参数决定搜索范围

### 3. 百分比转换层：Skill filter 内部
- LLM 传百分比（50 = 50%），Skill `filter()` 内部 ×10 转千分比（500‰）
- 转换逻辑集中在 `search_by_effect` 和 `search_by_skill_effect` 的 filter 方法中

### 4. 路由 Prompt
- 新增规则 10：说明 `targetType` 和 `minValue` 的使用场景
- 新增 2 个 few-shot 示例引导 LLM 正确输出

## 影响
- 数据库体积略增（每个效果多 3 个 int 字段）
- Token 消耗不变（新字段不进入 LLM context）
- 向后兼容：`minValue`/`maxValue` 为可选参数，不传时行为与之前一致
