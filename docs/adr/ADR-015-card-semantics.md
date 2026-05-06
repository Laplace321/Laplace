# ADR-015: "蓝卡配卡"语义理解优化

- **日期**: 2026-05-06
- **状态**: 待修复

## 背景

用户查询: "有哪些蓝卡配卡、带有无敌贯通技能、且稀有度四星以上的剑阶从者?"

**问题**: 千子村正(蓝卡宝具 + 无敌贯通 + 5星 Saber)没有被识别出来

## 根因分析

### LLM 意图解析错误

**用户语义**: "蓝卡配卡" = 宝具是蓝卡 (npCard = arts)

**LLM 解析结果**: 
```json
{
  "cards": {
    "arts": 3
  }
}
```

LLM 错误地将"蓝卡配卡"理解为"指令卡有3张蓝卡",而不是"宝具是蓝卡"。

### 数据模型层面

当前数据模型设计是正确的:
- `cards`: 指令卡构成 (如 `{"arts": 2, "buster": 2, "quick": 1}`)
- `npCard`: 宝具颜色 (如 `"arts"`, `"buster"`, `"quick"`)

查询执行器 `_filter_cards` 也正确处理了这两个字段:
```python
# 配卡 (指令卡数量)
if cards is not None:
    for card_type, count in cards.items():
        if servant_cards.get(card_type, 0) < count:
            return False

# 宝具颜色
if np_card is not None:
    if servant.get("npCard", "") != np_card:
        return False
```

**问题不在代码逻辑,而在 LLM 的语义理解**。

## FGO 玩家社区约定

在 FGO 玩家社区中:

| 术语 | 含义 | 对应字段 |
| :--- | :--- | :--- |
| "蓝卡从者" | 宝具是蓝卡 | `npCard: "arts"` |
| "蓝卡配卡" | 宝具是蓝卡 | `npCard: "arts"` |
| "3蓝配卡" | 指令卡有3张蓝卡 | `cards: {"arts": 3}` |
| "蓝卡队" | 蓝卡宝具的从者组成的队伍 | `npCard: "arts"` |

## 影响范围

### 受影响的查询

所有包含以下表述的查询都会被错误解析:
- "蓝卡配卡的从者"
- "红卡从者"
- "绿卡宝具"
- "蓝卡队成员"

### 受影响的从者示例

以千子村正为例:
- 实际数据: `npCard: "arts"`, `cards: {"arts": 2, "buster": 2, "quick": 1}`
- 用户查询: "蓝卡配卡"
- LLM 解析: `cards: {"arts": 3}` (错误!)
- 正确解析: `npCard: "arts"`

## 解决方案

### 方案 A: 优化 System Prompt (推荐)

在 LLM 的意图解析 Prompt 中明确说明语义约定:

```markdown
## 配卡相关术语说明

- **"蓝卡配卡" / "蓝卡从者" / "蓝卡宝具"**: 指宝具颜色为蓝卡 → `{"npCard": "arts"}`
- **"红卡配卡" / "红卡从者"**: 指宝具颜色为红卡 → `{"npCard": "buster"}`
- **"绿卡配卡" / "绿卡从者"**: 指宝具颜色为绿卡 → `{"npCard": "quick"}`

- **"N蓝配卡" (N为数字)**: 指指令卡有N张蓝卡 → `{"cards": {"arts": N}}`
  - 例: "3蓝配卡" → `{"cards": {"arts": 3}}`
  - 例: "2红配卡" → `{"cards": {"buster": 2}}`

注意: 用户说"蓝卡配卡"通常指宝具颜色,不是指令卡数量!
```

### 方案 B: 增加 Few-shot 示例

在 Prompt 中添加具体示例:

```json
{
  "examples": [
    {
      "query": "蓝卡配卡的从者有哪些",
      "intent": {"npCard": "arts"}
    },
    {
      "query": "3蓝配卡的从者",
      "intent": {"cards": {"arts": 3}}
    },
    {
      "query": "红卡从者",
      "intent": {"npCard": "buster"}
    }
  ]
}
```

### 方案 C: 后处理修正 (临时方案)

在 LLM 返回后,增加一层语义修正:

```python
def fix_card_semantics(intent: dict) -> dict:
    """修正配卡语义误解。"""
    cards = intent.get("cards")
    if cards and isinstance(cards, dict):
        # 如果用户没有明确说"N张蓝卡",但LLM解析成了数字
        # 可能是误解了"蓝卡配卡"
        # 这个方案较复杂,不推荐
        pass
    return intent
```

## 决策建议

### 推荐: 方案 A + B 组合

**理由**:
1. **根源解决**: 从 Prompt 层面纠正 LLM 的语义理解
2. **成本低**: 只需修改 `prompts.py`,无需改动数据模型
3. **可扩展**: 可以同时处理其他类似的语义歧义

**实施步骤**:
1. 在 `prompts.py` 的 System Prompt 中添加配卡术语说明
2. 添加 3-5 个 Few-shot 示例
3. 测试验证:
   - "蓝卡配卡的从者" → `{"npCard": "arts"}` ✅
   - "3蓝配卡的从者" → `{"cards": {"arts": 3}}` ✅
   - "红卡从者" → `{"npCard": "buster"}` ✅

### 风险提示

- **LLM 稳定性**: 即使优化了 Prompt,LLM 仍可能有偶发误解
- **用户表述多样性**: 玩家可能用各种说法("蓝卡队"、"蓝光炮"等)

**缓解措施**:
- 收集用户查询日志,持续优化 Prompt
- 考虑在前端增加快捷筛选按钮,减少自然语言歧义

## 相关文档

- ADR-014: NP 充能数据模型优化
- `server/query_executor.py` - `_filter_cards` 函数
- `server/prompts.py` - System Prompt 模板

## 待确认

1. 是否还有其他类似的术语歧义? (如"光炮"指宝具类型还是效果?)
2. 是否需要在前端提供结构化筛选器作为补充?
