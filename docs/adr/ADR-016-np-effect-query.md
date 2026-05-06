# ADR-016: 宝具效果查询支持

- **日期**: 2026-05-06
- **状态**: 待实现

## 背景

用户查询: "宝具能给全队加攻的从者有哪些?"

**问题**: 返回了 137 个从者,包含所有**技能**有加攻的从者,而不是**宝具**有加攻的从者。

## 根因分析

### 1. LLM 意图解析缺少宝具效果字段

当前 `QueryConditions` (schemas.py) 只定义了技能效果:

```python
class QueryConditions(BaseModel):
    skillEffect: str | None = None          # 单个技能效果
    skillEffects: list[str] | None = None   # 多个技能效果
    skillEffectsOp: Literal["and", "or"] | None = None
    targetType: Literal["self", "party", "enemy"] | None = None
    # ... 其他字段
```

**缺失字段**:
- `npEffect`: 宝具效果 (单个)
- `npEffects`: 宝具效果 (多个)

### 2. LLM 无法区分"技能加攻"和"宝具加攻"

当用户说"宝具加攻"时,LLM 只能使用 `skillEffect: "upAtk"`,因为没有 `npEffect` 字段可用。

**用户语义**: 宝具效果有攻击力提升
**LLM 解析**: `{"skillEffect": "upAtk", "targetType": "party"}`
**实际查询**: 所有技能有 upAtk 的从者 (137个)
**期望查询**: 所有宝具有 upAtk 的从者 (41个)

### 3. 数据模型已支持,但查询执行器未实现

数据库已有 `npEffects` 字段:

```python
# server/data_loader.py
entry = {
    "skillEffects": sorted(list(skill_effects)),  # 技能效果
    "npEffects": sorted(list(np_effects_set)),    # 宝具效果
    # ...
}
```

但 `query_executor.py` 中没有宝具效果的过滤器。

## 影响范围

### 受影响的查询

所有包含以下表述的查询都会被错误处理:
- "宝具有加攻效果的从者"
- "宝具带无敌的从者"
- "宝具能给队友充能的从者"
- "宝具有特攻的从者"

### 数据统计

以"加攻"为例:
- **技能加攻**: 137 个从者
- **宝具加攻**: 41 个从者
- **交集**: 部分从者两者都有

错误查询返回了技能加攻的结果,严重偏离用户意图。

## 解决方案

### 方案 A: 完整实现宝具效果查询 (推荐)

#### Step 1: 扩展 Schema

```python
class QueryConditions(BaseModel):
    # 技能效果 (现有)
    skillEffect: str | None = None
    skillEffects: list[str] | None = None
    skillEffectsOp: Literal["and", "or"] | None = None
    
    # 宝具效果 (新增)
    npEffect: str | None = None
    npEffects: list[str] | None = None
    npEffectsOp: Literal["and", "or"] | None = None
    
    # 目标类型 (技能和宝具共用)
    targetType: Literal["self", "party", "enemy"] | None = None
```

#### Step 2: 实现宝具效果过滤器

```python
@register_filter("npEffect", "npEffects")
def _filter_np_effect(servant: dict, conditions: dict) -> bool:
    """宝具效果筛选。"""
    # 单个效果
    np_effect = conditions.get("npEffect")
    if np_effect is not None:
        if np_effect not in servant.get("npEffects", []):
            return False
    
    # 多个效果
    np_effects = conditions.get("npEffects")
    if np_effects is not None:
        op = conditions.get("npEffectsOp", "and")
        servant_np_effects = set(servant.get("npEffects", []))
        
        if op == "and":
            if not all(eff in servant_np_effects for eff in np_effects):
                return False
        else:  # or
            if not any(eff in servant_np_effects for eff in np_effects):
                return False
    
    return True
```

#### Step 3: 更新 LLM Prompt

在 System Prompt 中明确区分技能和宝具效果:

```markdown
## 效果查询说明

- **技能效果**: 使用 `skillEffect` 或 `skillEffects`
  - 例: "有无敌技能的从者" → `{"skillEffect": "invincible"}`
  
- **宝具效果**: 使用 `npEffect` 或 `npEffects`
  - 例: "宝具有加攻效果的从者" → `{"npEffect": "upAtk"}`
  - 例: "宝具带无敌和加攻的从者" → `{"npEffects": ["invincible", "upAtk"]}`

- **目标类型**: `targetType` 适用于技能和宝具
  - `self`: 给自己
  - `party`: 给全队
  - `enemy`: 给敌人
```

#### Step 4: 添加 Few-shot 示例

```json
{
  "examples": [
    {
      "query": "宝具有加攻效果的从者",
      "intent": {"npEffect": "upAtk"}
    },
    {
      "query": "宝具能给全队充能的从者",
      "intent": {"npEffect": "gainNp", "targetType": "party"}
    },
    {
      "query": "有无敌技能的从者",
      "intent": {"skillEffect": "invincible"}
    },
    {
      "query": "宝具带无敌和特攻的从者",
      "intent": {"npEffects": ["invincible", "specialAttack"], "npEffectsOp": "and"}
    }
  ]
}
```

### 方案 B: 快速修复 (临时方案)

在现有 `skillEffect` 基础上,增加一个布尔字段区分:

```python
class QueryConditions(BaseModel):
    skillEffect: str | None = None
    isNpEffect: bool = False  # 新增:是否为宝具效果
    targetType: Literal["self", "party", "enemy"] | None = None
```

**缺点**: 
- 语义不清晰
- 不支持宝具效果的多条件查询
- 向后兼容性差

### 方案 C: 智能推断 (不推荐)

在查询执行器中自动判断:
- 如果用户说"宝具",使用 `npEffects`
- 否则使用 `skillEffects`

**缺点**:
- 规则复杂,容易出错
- 不如让 LLM 直接输出正确字段

## 决策建议

### 推荐: 方案 A

**理由**:
1. **架构清晰**: 技能和宝具效果物理分离,语义明确
2. **可扩展**: 支持宝具效果的多条件查询 (AND/OR)
3. **一致性**: 与技能效果的查询模式保持一致
4. **数据已就绪**: 数据库已有 `npEffects` 字段,无需修改数据层

**实施步骤**:
1. 修改 `schemas.py`: 添加 `npEffect`, `npEffects`, `npEffectsOp` 字段
2. 修改 `query_executor.py`: 实现 `_filter_np_effect` 过滤器
3. 修改 `prompts.py`: 更新 System Prompt,添加宝具效果说明和示例
4. 更新前端: 在调试面板显示宝具效果匹配情况
5. 添加回归测试: 覆盖宝具效果查询的各种场景

**工作量评估**: 
- Schema 修改: 10 行
- 过滤器实现: 20 行
- Prompt 优化: 30 行
- 测试: 5 个测试用例
- **总计**: 约 2-3 小时

## 相关文档

- ADR-015: "蓝卡配卡"语义理解优化
- ADR-009: Filter Registry 模式
- `server/schemas.py` - QueryConditions 定义
- `server/query_executor.py` - 过滤器实现
- `server/data_loader.py` - npEffects 数据提取

## 待确认

1. 宝具效果是否也需要类似技能的"目标类型"精确匹配?
   - 当前 `npEffects` 是集合,不包含 targetType 信息
   - 如果需要,需修改 `data_loader.py` 的数据结构

2. 是否需要支持"技能或宝具有某效果"的 OR 查询?
   - 例: "有加攻效果的从者"(技能或宝具都可以)
   - 可以在 Prompt 中引导用户明确表述

3. 宝具的 targetType 如何提取?
   - 宝具的 `funcTargetType` 通常是固定的(如伤害宝具都是 enemy)
   - 辅助型宝具可能有 party/self
   - 需要检查 `data_loader.py` 的宝具解析逻辑
