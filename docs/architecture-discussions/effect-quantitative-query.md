# 架构讨论：效果量化筛选 & 指向性筛选

> 状态：**讨论中** | 触发：trace `64947757` | 日期：2026-05-09

## 一、问题描述

用户查询：「请查找一下**给队友**增加**红魔放超过 50%** 的从者有哪些」

该查询包含 3 个筛选维度：

| 维度 | 用户意图 | 当前支持 | 差距 |
|:--|:--|:--|:--|
| 效果类型 | Buster 提升 (upBuster) | ✅ 支持 | — |
| 效果数值 | > 50% | ❌ 不支持 | MV 只存有/无，不存数值 |
| 技能指向 | 给队友（排除 self） | ⚠️ 半支持 | 数据层有 targetType，但 LLM 不知道可以传；路由 Prompt 缺乏引导 |

实际执行结果：返回 178 个"有 Buster 提升"的从者，数值和指向条件全部丢失。

## 二、根因分析

### 2.1 数据层：效果数值在 MV 构建时被丢弃

`data_loader.py → extract_skill_effects()` 当前构建的 `skillDetails`:

```python
# 当前输出结构
{
    "skillName": "Charisma B",
    "skillNum": 1,
    "effects": [
        {"type": "upAtk", "funcType": "addStateShort", "targetType": "party"}
    ]
}
```

**缺失字段**：`svals.Value`（效果数值）、`svals.Turn`（持续回合）在 MV 中完全未保留。

原始数据中这些信息是存在的（`skills[].functions[].svals.Value`，万分比）。

### 2.2 Skill 参数契约：不支持数值条件

`search_by_effect` 的 Params 契约：

```python
class Params(BaseModel):
    effect: str | None         # 效果名
    effects: list[str] | None  # 多效果
    effects_op: str            # and/or
    source: str                # skill/np/both
    target_type: str | None    # self/party/enemy  ← 有字段！
```

- `target_type` **字段已存在**，`_match_effect` 也支持按 targetType 过滤
- 但路由 Prompt **没有引导 LLM 输出这个参数**，所以 LLM 从来不传
- 完全缺少 `min_value` / `op` + `value` 等数值条件参数

### 2.3 路由层：LLM 尝试传数值参数但被执行层丢弃

从 trace 日志可见：

```json
// routing_output — LLM 实际输出
{"skill_name": "search_by_skill_effect", "params": {"skillEffect": "upBuster", "op": "gt", "value": 50}}

// execution — 执行层 Pydantic 校验后只保留了 effect
{"accepted_skills": [{"skill_name": "search_by_skill_effect", "params": {"effect": "upBuster"}}]}
```

LLM 很聪明地尝试传了 `op` 和 `value`，但 Pydantic 契约中没定义这两个字段，被静默丢弃了。

### 2.4 交叉筛选：需要同一 function 内的三维 AND

用户问的是"给队友 Buster 提升 > 50%"，意思是**同一个技能函数**必须同时满足：
- 效果 = upBuster
- 数值 > 50%（即 svals.Value > 5000，万分比）
- 指向 = party（funcTargetType ∈ ptAll/ptOne）

如果分开匹配，会出现误判：从者 A 有自身 Buster +50%（self）+ 给队友攻击力 +10%（ptAll），会被错误命中。

当前 `_match_effect` 的实现是遍历 `skillDetails`，按 effect + targetType 二维过滤。需要扩展为三维。

## 三、方案设计

### 3.1 数据层改造：扩展 skillDetails 结构

在 `extract_skill_effects()` 中，为每个效果记录追加 `value`、`turn`、`count` 字段：

```python
# 目标输出结构
{
    "skillName": "Charisma B",
    "skillNum": 1,
    "effects": [
        {
            "type": "upAtk",
            "funcType": "addStateShort",
            "targetType": "party",
            "value": 180,       # 新增：Lv1 数值（万分比）
            "valueMax": 360,    # 新增：Lv10 数值（万分比）
            "turn": 3,          # 新增：持续回合
            "count": -1         # 新增：次数限制（-1=无限制）
        }
    ]
}
```

**数据来源**：`svals` 字段。

**方案选择 — Lv1 vs Lv10 vs 全等级**：

| 方案 | 数据量 | 优势 | 劣势 |
|:--|:--|:--|:--|
| A: 只存 Lv10（valueMax） | +0 存储 | 最简洁；玩家通常按满级查 | 无法支持"未满级"场景 |
| B: 存 Lv1 + Lv10 | 2x | 可展示范围区间 | 稍复杂 |
| C: 存全10级 | 10x | 最精确 | 数据膨胀严重，实用性低 |

**建议**：方案 B（Lv1 + Lv10），兼顾查询精度和数据量。

### 3.2 Skill 参数契约扩展

在 `search_by_effect` 和 `search_by_skill_effect` 的 Params 中新增：

```python
class Params(BaseModel):
    effect: str | None
    effects: list[str] | None
    effects_op: str = "and"
    source: str = "both"
    target_type: str | None = Field(default=None, alias="targetType")
    # ── 新增：数值条件 ──
    min_value: int | None = Field(default=None, alias="minValue",
        description="效果最小数值（百分比，如50表示50%）")
    max_value: int | None = Field(default=None, alias="maxValue",
        description="效果最大数值")
```

**用百分比而非万分比**：LLM 输入用人类直觉的百分比（50），执行层内部转换为万分比（5000）。

### 3.3 执行层改造：`_match_effect` 三维过滤

```python
def _match_effect(servant, effect_name, target_type=None, min_value=None, max_value=None):
    # 快速路径
    if effect_name not in servant.get("skillEffects", []):
        return False

    # 如果有数值/指向条件，遍历 skillDetails 精细过滤
    if target_type is not None or min_value is not None or max_value is not None:
        for skill in servant.get("skillDetails", []):
            for eff in skill.get("effects", []):
                if eff.get("type") != effect_name:
                    continue
                if target_type and eff.get("targetType") != target_type:
                    continue
                value = eff.get("valueMax", 0)
                if min_value and value < min_value * 100:  # 百分比→万分比
                    continue
                if max_value and value > max_value * 100:
                    continue
                return True
        return False

    return True
```

### 3.4 路由 Prompt 补充

在路由 Prompt 的 `search_by_effect` 参数说明中补充：

```
- targetType (可选): 效果目标 "self"(自身) / "party"(队友/全队) / "enemy"(敌方)
  示例：「给队友加攻」→ targetType="party"
- minValue (可选): 效果最小数值（百分比），如 50 表示 ≥50%
  示例：「超过50%的Buster提升」→ minValue=50
```

## 四、影响评估

### 4.1 数据量影响

当前 `servants_db.json` 约 5MB。`skillDetails` 每条记录新增 4 个数值字段（value/valueMax/turn/count），预估增长 ~10-15%（+500KB~750KB）。可接受。

### 4.2 性能影响

- 无数值条件时走快速路径（`effect in skillEffects`），零影响
- 有数值条件时遍历 `skillDetails`，这已经是 `target_type` 筛选的现有路径，无额外开销
- 无 Token 消耗增长（路由 Prompt 只加 2 行参数说明）

### 4.3 向后兼容

- `skillEffects` 扁平集合保持不变，现有 Skill 不受影响
- `skillDetails` 只是扩展字段，不删不改已有字段
- 路由 Prompt 新增参数是可选的，不传时行为完全不变

### 4.4 解锁的新查询场景

- 「给全队加攻超过 30% 的从者」→ `upAtk, minValue=30, targetType=party`
- 「有 3 回合无敌的从者」→ 未来可扩展 `minTurn` 参数
- 「自身暴击星集中超过 500% 的从者」→ `upStarweight, minValue=500, targetType=self`
- 「能给单体充 NP 超过 50% 的辅助」→ NP 充能已有 `maxPtOneCharge` MV，但效果类查询可复用

## 五、分步实施计划

### Step 1：数据层 — 扩展 skillDetails（MV 重建）
- 修改 `data_loader.py → extract_skill_effects()`，提取 `svals` 数值
- 处理 `svals` 的多级数组（取 Lv1/Lv10）
- 重新运行 `data_loader` 生成新 `servants_db.json`
- 回归测试验证数据完整性

### Step 2：执行层 — 扩展 `_match_effect` 支持数值过滤
- 修改 `query_executor.py → _match_effect`，增加 `min_value`/`max_value` 参数
- 百分比→万分比的转换逻辑

### Step 3：Skill 契约 — 扩展参数定义
- 修改 `search_by_effect` 和 `search_by_skill_effect` 的 Params
- filter() 方法传递新参数给 `_match_effect`

### Step 4：路由层 — Prompt 引导 LLM 输出新参数
- 补充 `targetType`、`minValue` 参数说明和示例
- 确保 LLM 在用户提到"给队友"/"超过XX%"时输出正确参数

### Step 5：前端预消化 — `_describe_filters` 补充
- 中文描述支持数值和指向（如"技能效果包含「给队友的 Buster 提升 ≥50%」"）

## 六、决策结论（2026-05-09 达成）

| # | 问题 | 决策 |
|:--|:--|:--|
| 1 | svals 取值策略 | **方案 A：只存 Lv10（valueMax）**。最简洁，玩家通常按满级查 |
| 2 | 宝具效果量化 | **暂不做**。宝具有 OC 1~5 级复杂度更高，记入需求描述 Phase 8 独立迭代 |
| 3 | turn/count | **一并烘焙**。增量成本极小，数据层一次性做完 |
| 4 | 百分比转换层 | **在 Skill 的 filter() 中转换**。LLM 传百分比（50），filter 内部 ×100 转万分比（5000） |

> 状态变更：讨论中 → **结论已达成，进入实施**
