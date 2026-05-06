# ADR-014: NP 充能数据模型优化

- **日期**: 2026-05-06
- **状态**: 已实施

## 背景

当前 NP 充能数据模型存在语义混淆问题,以"C呆"(阿尔托莉雅·卡斯特)为例:

### 问题现象

前端显示: `30%+20%`
AI 文案: "她自身也拥有 50% 的自充能力"

### 实际情况

C呆的两个 NP 充能技能:
1. **Charisma of Hope B (技能2)**: 30% 群充 (targetType: party)
2. **Protection of the Lake A (技能3)**: 20% 自充 (targetType: self,但当前数据可能误标)

**核心矛盾**: 
- `totalSelfCharge = 50%` 是将"给队友充能"和"给自己充能"混算的结果
- 前端显示 `30%+20%` 让人误以为是一个技能有"50%自充"
- 实际上这是**两个独立技能**的充能能力,且目标对象不同

## 当前数据模型缺陷

### 1. 数据提取层 (`data_loader.py` L265)

```python
total_self_charge = sum(self_charges) + sum(party_charges)
```

**问题**: 
- 字段名 `totalSelfCharge` 暗示"自充总量",但实际包含了"群充"
- 语义混乱: "自充"应该只指 targetType=self 的充能

### 2. 前端展示层 (`demo/app.js` L369)

```javascript
const charges = servant.npCharges.map(c => `${c.chargePercent}%`).join("+");
```

**问题**:
- 将所有充能值简单相加展示,丢失了"目标类型"信息
- 用户无法区分"30%群充 + 20%自充"和"50%自充"的本质差异

### 3. AI 生成层 (`main.py`)

AI 基于 `totalSelfCharge` 生成文案时,会错误地描述为"50%自充能力"

## 影响范围

### 受影响的从者类型

1. **辅助型从者** (如 C呆、孔明、梅林):
   - 主要价值在于群充能力
   - 自充可能是次要能力甚至不存在

2. **复合型从者** (如某些高星从者):
   - 同时拥有自充和群充技能
   - 需要明确区分两种能力

3. **纯自充型从者**:
   - 只给自己充能,不受影响

## 解决方案

### 方案 A: 数据模型重构 (推荐)

#### 1. 修改数据提取逻辑

```python
# 分离自充和群充
self_charges = [c["chargePercent"] for c in charges if c["targetType"] == "self"]
party_charges = [c["chargePercent"] for c in charges if c["targetType"] == "party"]

# 新增字段
entry = {
    # ... 其他字段 ...
    
    # 自充相关
    "selfCharges": self_charges,  # [20]
    "maxSelfCharge": max(self_charges) if self_charges else 0,  # 20
    "totalSelfCharge": sum(self_charges),  # 20
    
    # 群充相关 (新增)
    "partyCharges": party_charges,  # [30]
    "maxPartyCharge": max(party_charges) if party_charges else 0,  # 30
    "totalPartyCharge": sum(party_charges),  # 30
    
    # 详细充能列表 (保留,但优化结构)
    "npCharges": charges,  # [{skillName, chargePercent, targetType}, ...]
}
```

#### 2. 优化前端展示

```javascript
// 分离展示自充和群充
const selfCharges = servant.npCharges.filter(c => c.targetType === 'self');
const partyCharges = servant.npCharges.filter(c => c.targetType === 'party');

let chargeDisplay = '';
if (selfCharges.length > 0) {
  const selfSum = selfCharges.reduce((sum, c) => sum + c.chargePercent, 0);
  chargeDisplay += `自充${selfSum}%`;
}
if (partyCharges.length > 0) {
  const partySum = partyCharges.reduce((sum, c) => sum + c.chargePercent, 0);
  chargeDisplay += `群充${partySum}%`;
}
```

#### 3. AI 上下文优化

```python
# 在构建 LLM context 时,分别提供自充和群充信息
context["self_charge"] = {
    "total": servant["totalSelfCharge"],
    "skills": [c for c in servant["npCharges"] if c["targetType"] == "self"]
}
context["party_charge"] = {
    "total": servant["totalPartyCharge"],
    "skills": [c for c in servant["npCharges"] if c["targetType"] == "party"]
}
```

### 方案 B: 保留旧字段,新增区分字段 (向后兼容)

如果担心破坏现有查询逻辑:

```python
entry = {
    # 保留旧字段 (向后兼容)
    "maxSelfCharge": max(self_charges) if self_charges else 0,
    "totalSelfCharge": sum(self_charges) + sum(party_charges),  # 旧逻辑
    
    # 新增精确字段
    "selfOnlyCharge": sum(self_charges),  # 纯自充
    "partyCharge": sum(party_charges),  # 群充
    "npChargesDetailed": charges,  # 带 targetType 的详细列表
}
```

### 方案 C: 仅修改前端展示逻辑 (快速修复)

最小改动方案:

```javascript
// 在前端根据 targetType 分别显示
const charges = servant.npCharges.map(c => {
  const type = c.targetType === 'self' ? '自充' : '群充';
  return `${type}${c.chargePercent}%`;
}).join(' + ');
// 结果: "群充30% + 自充20%"
```

## 决策建议

### 推荐: 方案 A (数据模型重构)

**理由**:
1. **语义清晰**: 彻底解决"自充"和"群充"的概念混淆
2. **长期收益**: 为未来的"充能辅助查询"(如"找群充30%以上的从者")打下基础
3. **数据完整性**: 保留所有维度的信息,不丢失任何语义

**实施步骤**:
1. 修改 `data_loader.py` 的数据提取逻辑
2. 重新运行 `python3 -m server.data_loader` 刷新数据库
3. 更新前端 `app.js` 的展示逻辑
4. 更新 `main.py` 中构建 LLM context 的逻辑
5. 更新 Prompt 模板,明确区分自充和群充的表述
6. 添加回归测试覆盖新字段

### 风险评估

- **向后兼容性**: 旧字段 `totalSelfCharge` 的值会改变,影响基于该字段的查询
- **前端适配**: 需要更新卡片展示逻辑
- **AI Prompt**: 需要调整描述方式

**缓解措施**:
- 保留旧字段名,但修正其语义(改为 `totalSelfCharge` 仅包含 self)
- 前端使用渐进式更新,先显示详细列表,再优化汇总文案

## 待确认问题

1. **数据准确性**: Protection of the Lake A 的 targetType 是否真的是 self?
   - 需要检查 Atlas Academy 原始数据
   - 可能需要修正 `data_loader.py` 中的 targetType 判定逻辑

2. **业务定义**: 
   - "自充"在 FGO 玩家社区中是否包含"单技能给自己充能"?
   - "群充"是否包含"给自己+队友"的混合型充能?

3. **查询需求**:
   - 用户是否会查询"纯自充型从者"(只给自己充能,不给队友)?
   - 是否需要支持"群充X%以上"的筛选条件?

## 参考

- FGO 技能分类: 自充(Self NP Charge) vs 群充(Party NP Charge)
- Chaldea 数据模型: `FuncTargetType` 的精确分类
- 玩家社区约定: "自充"通常指 targetType=self 的技能
