# ADR-017: 从者数据模型的 AI-Ready 重构思考

- **日期**: 2026-05-06
- **状态**: 讨论中
- **触发问题**: ADR-014 (NP 充能语义混淆) + ADR-015 (配卡语义误解) + ADR-016 (宝具效果查询缺失)

## 背景与动机

### 当前困境

在过去两天的用户测试中,连续暴露了三个**语义理解层面的问题**:

1. **ADR-014**: "C呆 50%充能" — 自充 vs 群充的概念混淆
2. **ADR-015**: "蓝卡配卡" — 指令卡 vs 宝具颜色的语义歧义
3. **ADR-016**: "宝具加攻" — 技能效果 vs 宝具效果的查询混淆

**共同根因**: 当前数据模型是**"面向查询执行"的**,而非**"面向 AI 理解"的**。

### 传统数据模型 vs AI-Ready 数据模型

#### 传统数据模型 (Current)

```python
# 面向数据库查询和前端展示
{
  "id": 504500,
  "name": "Altria Caster",
  "className": "caster",
  "rarity": 5,
  "npCharges": [{"skillName": "...", "chargePercent": 30, "targetType": "party"}],
  "totalSelfCharge": 50,  # ← 语义混乱:混合了自充和群充
  "skillEffects": ["gainNp", "upAtk", ...],
  "npEffects": ["upAtk", ...],
  "cards": {"arts": 2, "buster": 2, "quick": 1},
  "npCard": "arts"
}
```

**问题**:
- 字段命名从**开发者视角**出发 (`totalSelfCharge`)
- 语义边界模糊 (自充/群充混算)
- AI 需要额外推理才能理解业务含义

#### AI-Ready 数据模型 (Target)

```python
# 面向 AI 语义理解和自然语言交互
{
  "identity": {
    "id": 504500,
    "names": {
      "en": "Altria Caster",
      "cn": "阿尔托莉雅·卡斯特",
      "jp": "アルトリア・キャスター",
      "community_nicknames": ["C呆", "术呆", "光呆"]
    },
    "class": {
      "id": "caster",
      "name_cn": "术阶",
      "name_en": "Caster"
    },
    "rarity": 5
  },
  
  "role_profile": {
    "primary_role": "support",  # 辅助/输出/副核
    "charge_capability": {
      "self_charge": {
        "max_percent": 20,
        "skills": ["Protection of the Lake A"],
        "description_cn": "技能自充20%"
      },
      "party_charge": {
        "max_percent": 30,
        "skills": ["Charisma of Hope B"],
        "description_cn": "群充30%"
      },
      "community_label": "50%充能拐"  # 玩家社区说法
    }
  },
  
  "combat_profile": {
    "deck_composition": {
      "cards": {"arts": 2, "buster": 2, "quick": 1},
      "np_card": {
        "color": "arts",
        "name_cn": "蓝卡",
        "community_term": "蓝卡从者"
      }
    },
    "effect_capabilities": {
      "skills": {
        "offensive": ["攻击力提升", "Arts性能提升"],
        "defensive": ["无敌"],
        "support": ["NP增加(群)", "NP增加(自)"]
      },
      "noble_phantasm": {
        "effects": ["攻击力提升", "Arts性能提升"],
        "target": "全队"
      }
    }
  }
}
```

**优势**:
- 字段命名从**玩家/AI视角**出发
- 语义边界清晰 (self_charge vs party_charge)
- 包含社区术语映射,降低 LLM 推理成本
- 层次化结构,符合人类认知模式

## AI-Ready 数据模型设计原则

### 原则 1: 语义显式化 (Explicit Semantics)

**传统模型**: 让 AI 推理
```python
"totalSelfCharge": 50  # AI 需要推断:这是自充+群充的总和?
```

**AI-Ready 模型**: 显式声明
```python
"charge_capability": {
  "self_charge": {"max_percent": 20},
  "party_charge": {"max_percent": 30},
  "total_combined": 50,  # 明确说明是合计值
  "community_label": "50%充能"  # 玩家实际说法
}
```

### 原则 2: 业务术语映射 (Business Terminology Mapping)

**传统模型**: 技术枚举
```python
"className": "caster"
"npCard": "arts"
```

**AI-Ready 模型**: 多层映射
```python
"class": {
  "id": "caster",
  "display_cn": "术阶",
  "display_en": "Caster",
  "community_terms": ["术阶", "C阶", "C"]
}
"np_card": {
  "id": "arts",
  "display_cn": "蓝卡",
  "community_terms": ["蓝卡从者", "蓝卡配卡", "蓝光炮"]
}
```

### 原则 3: 角色定位显式化 (Role Profiling)

FGO 从者在玩家社区中有明确的**角色分工**:

| 角色类型 | 定义 | 典型从者 | 充能特征 |
| :--- | :--- | :--- | :--- |
| **单核** | 独立输出核心 | 红卡光炮 | 高自充(50%+),无群充 |
| **多核** | 能输出能辅助 | 蓝卡光炮 | 自充+群充混合 |
| **辅助(拐)** | 纯辅助 | 孔明、C呆 | 高群充,自充次要 |
| **副核** | 辅助兼输出 | 杀狐 | 中等自充+群充 |

**AI-Ready 设计**:
```python
"role_profile": {
  "primary_role": "support",
  "secondary_role": "self_sufficient",
  "role_description_cn": "充能辅助型从者,兼具自充能力",
  "meta_tags": ["50充拐", "蓝卡队核心", "周回必备"]
}
```

### 原则 4: 查询意图预编译 (Intent Pre-compilation)

**传统模型**: LLM 实时解析
```
用户: "蓝卡配卡的从者"
→ LLM 解析: {"cards": {"arts": 3}}  ❌ 错误
→ 执行查询: 0 结果
```

**AI-Ready 模型**: 预编译常见查询模式
```python
"query_optimizations": {
  "by_np_card_color": "arts",  # 预编译:可直接用于过滤
  "by_charge_type": ["party_charge"],  # 群充型从者
  "by_role": ["support", "self_sufficient"],
  "searchable_by": ["C呆", "术呆", "蓝卡拐", "50充"]
}
```

### 原则 5: 分层可解释性 (Layered Explainability)

**传统模型**: 扁平结构
```python
{
  "skillEffects": ["gainNp", "upAtk", "invincible"],
  "npEffects": ["upAtk"]
}
```

**AI-Ready 模型**: 分层 + 业务解释
```python
"effect_capabilities": {
  "skills": {
    "by_category": {
      "charge": [
        {
          "effect": "gainNp",
          "target": "party",
          "max_value": 30,
          "skill_name": "希望のカリスマ B",
          "description_cn": "群充30%"
        }
      ],
      "offensive": [...],
      "defensive": [...]
    },
    "meta_summary": {
      "is_charge_support": true,
      "charge_type": "party",
      "has_survival_skills": true
    }
  }
}
```

## 充能数据模型的深度思考

### FGO 游戏机制层面

在 FGO 中,"充能"有**三种完全不同的业务含义**:

#### 1. 自充 (Self NP Charge)
- **定义**: 技能给自己充能
- **业务价值**: 从者启动速度,能否单走
- **玩家关注**: "能不能30/50自充开局开宝具?"
- **典型查询**: "有30自充的从者"

#### 2. 群充 (Party NP Charge)
- **定义**: 技能给全队充能
- **业务价值**: 队伍充能辅助能力
- **玩家关注**: "能不能当充能拐?"
- **典型查询**: "能给队友充能的从者"

#### 3. 总充能 (Combined Charge)
- **定义**: 自充 + 群充的数值总和
- **业务价值**: **这是玩家社区的简化说法**
- **实际含义**:
  - 对于**辅助从者**: "50充" = 30群充 + 20自充 → 强调辅助能力
  - 对于**单核从者**: "50充" = 50自充 → 强调启动能力
  - **同一数值,不同语义!**

### 当前模型的问题

```python
"totalSelfCharge": 50  # ← 字段名暗示"自充",实际包含群充
```

**语义矛盾**:
- 字段名: `totalSelfCharge` (总自充)
- 实际值: `self_charge(20) + party_charge(30) = 50`
- 玩家理解: "50充" (但含义因角色定位而异)

### AI-Ready 设计建议

#### 方案 A: 完全分离 (推荐)

```python
"charge_profile": {
  # 精确数据
  "self_charge": {
    "max_percent": 20,
    "skills": ["Protection of the Lake A"],
    "is_primary_feature": false  # 不是核心卖点
  },
  "party_charge": {
    "max_percent": 30,
    "skills": ["Charisma of Hope B"],
    "is_primary_feature": true  # 核心卖点
  },
  
  # 玩家视角
  "community_label": "30群充+20自充",
  "simplified_label": "50充",  # 仅用于展示,不用于查询
  "role_context": "充能辅助型",  # 帮助AI理解上下文
  
  # 查询优化
  "query_tags": ["party_charge_30", "self_charge_20", "combined_50"]
}
```

#### 方案 B: 角色化视图 (进阶)

为不同角色类型提供不同的"充能视图":

```python
"charge_views": {
  "as_support": {
    "highlight": "party_charge_30",
    "description": "群充30%,优秀充能拐",
    "query_field": "party_charge"
  },
  "as_self_sufficient": {
    "highlight": "self_charge_20",
    "description": "自充20%,需队友配合",
    "query_field": "self_charge"
  }
}
```

## 实施路线图

### Phase 1: 数据模型审计 (本周)

1. **盘点现有字段**:
   - 哪些字段语义模糊?
   - 哪些字段 AI 推理成本高?
   - 哪些字段缺少业务映射?

2. **玩家社区调研**:
   - 收集 FGO 玩家常用术语
   - 整理查询意图分类
   - 建立术语映射表

3. **Chaldea 模型参考**:
   - Chaldea 如何展示充能信息?
   - Chaldea 如何区分自充/群充?
   - 有哪些可借鉴的设计?

### Phase 2: 核心概念重构 (下周)

1. **充能模型重构**:
   - 分离 self_charge / party_charge
   - 添加 role_profile 字段
   - 添加 community_label 映射

2. **配卡模型重构**:
   - 明确 deck_composition (指令卡) vs np_card (宝具)
   - 添加 community_terms (蓝卡从者、红卡光炮等)

3. **效果分类重构**:
   - 分离 skill_effects vs np_effects
   - 按业务类别分组 (攻击/防御/辅助)
   - 添加效果描述和目标类型

### Phase 3: AI-Ready 优化 (下下周)

1. **查询意图预编译**:
   - 为常见查询模式添加优化字段
   - 减少 LLM 推理负担

2. **分层可解释性**:
   - 添加 meta_summary 字段
   - 提供业务层面的摘要信息

3. **术语映射完善**:
   - 建立完整的玩家术语 → 技术字段映射
   - 支持多种说法的同义词查询

### Phase 4: 验证与迭代 (持续)

1. **用户测试**:
   - 新模型是否减少了语义误解?
   - AI 回答是否更准确?

2. **性能监控**:
   - LLM Token 消耗是否降低?
   - 查询响应时间是否改善?

3. **持续优化**:
   - 根据用户反馈调整模型
   - 补充新的业务术语映射

## 关键决策点

### 决策 1: 数据冗余 vs 查询性能

**问题**: AI-Ready 模型会增加数据冗余(如同时存储精确值和社区标签)

**权衡**:
- ✅ **优点**: 降低 LLM 推理成本,减少 Token 消耗,提高准确率
- ❌ **缺点**: 数据体积增大,维护成本增加

**建议**: 优先保证 AI 理解准确性,冗余可接受

### 决策 2: 向后兼容性

**问题**: 重构后旧字段是否保留?

**方案**:
- **短期**: 保留旧字段,添加新字段(双写)
- **中期**: 逐步废弃旧字段
- **长期**: 完全迁移到新模型

### 决策 3: 数据生成策略

**问题**: 新字段是自动提取还是人工标注?

**建议**:
- **自动提取**: 从 Atlas Academy + Chaldea 数据中提取
- **规则映射**: 基于 FGO 游戏机制规则生成 role_profile
- **人工校对**: 社区术语映射需要人工维护

## 参考资源

### FGO 游戏机制
- NP 充能机制 (自充 vs 群充)
- 从者角色定位 (单核/多核/辅助)
- 玩家社区术语体系

### Chaldea 数据模型
- `lib/models/gamedata/servant.dart`
- `lib/models/gamedata/skill.dart`
- `lib/models/gamedata/func.dart`

### AI-Ready 数据设计
- 语义显式化原则
- 业务术语映射
- 分层可解释性

## 待确认问题

1. **充能的业务定义**:
   - "50充"在玩家社区中是否统一指代某种含义?
   - 还是需要结合从者类型理解?

2. **角色分类体系**:
   - 单核/多核/辅助/副核 的分类是否准确?
   - 是否需要更细粒度的分类?

3. **术语映射范围**:
   - 需要覆盖多少玩家社区术语?
   - 是否按服务器(日服/国服/美服)区分?

4. **数据更新策略**:
   - 新从者发布后,role_profile 如何自动生成?
   - 社区术语如何及时更新?

## 下一步行动

1. ✅ 创建本 ADR 文档,记录思考过程
2. ⏳ 调研 Chaldea 的充能展示逻辑
3. ⏳ 收集 FGO 玩家社区术语
4. ⏳ 设计新数据模型原型
5. ⏳ 小范围验证(3-5个从者)
6. ⏳ 全量数据迁移

---

**记录人**: AI Assistant  
**审核状态**: 待用户确认  
**优先级**: P1 (影响核心查询准确性)
