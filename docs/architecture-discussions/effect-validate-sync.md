# 架构讨论：Effect Validate 逻辑自动同步

> 状态：已结论 | 发起：2026-05-08 | 结论：2026-05-09

## 问题背景

在验收测试中发现千子村正的宝具效果被错误标注为"解除我方负面状态"和"赋予正面状态"，实际上这两个效果他并不具备。根因分析揭示了两个系统性架构缺陷。

## 问题 1：effect_schema 映射覆盖率严重不足

当前 `servants_db.json` 物理层包含的 funcType/buffType 远超 `effect_schema.json` 的映射范围：

| 维度 | DB 中出现 | schema 已映射 | 未映射 | 覆盖率 |
|:-----|:----------|:-------------|:-------|:-------|
| **funcType** | 44 种 | 40 种（含重复映射） | **21 种** | 52% |
| **buffType** | 109 种 | 44 种 | **68 种** | 39% |

### 未映射的 funcType（21 种）

```
absorbNpturn, addFieldChangeToField, addState, addStateShort,
addStateShortToField, cardReset, damageNp, delayNpturn,
displayBuffstring, extendBuffturn, fixCommandcard, hastenNpturn,
lossHp, lossHpPerSafe, lossHpSafe, lossNp, lossStar,
moveState, moveToLastSubmember, shortenBuffcount, transformServant
```

注：`addState`/`addStateShort` 是最常见的"加 buff"函数，它们本身不应被映射为独立效果（效果由其携带的 buff 决定），所以未映射是合理的。但 `damageNp`（宝具伤害）、`lossHp`（HP 减少）等可能需要评估。

### 未映射的 buffType（68 种，摘录关键）

```
downAtk, downDefence, downCriticaldamage, downCriticalpoint,
downCriticalrate, downNpdamage, downStarweight, downDropnp,
donotAct, donotNoble, donotSkill, donotRecovery,
changeCommandCardType, overwriteClassRelation, multiattack,
tdTypeChange, tdTypeChangeArts/Buster/Quick, ...
```

这些包括攻击力下降、防御力下降、眩晕、宝具封印、技能封印等常见效果。当 LLM 分析物理层数据时，会看到这些英文枚举值并自行猜测翻译，导致幻觉。

### 影响

1. **Materialized View 层（skillEffects/npEffects）**：只包含 schema 映射范围内的 55 种效果，覆盖率不足但至少不会产生错误标注
2. **物理层数据（skills/noblePhantasms）**：包含所有 funcType/buffType，LLM 直接分析时会遇到大量未翻译的英文枚举值

## 问题 2：validate 逻辑应源自 Chaldea 而非自行猜测

### 当前做法（错误）

`sync_chaldea.py` 从 Chaldea 的 `effect.dart` 提取 SkillEffect 定义时，**丢弃了 validate 函数**，只保留了 funcTypes/buffTypes 的简单映射。导致：

- `subState` funcType 同时映射了 `subState`、`subStatePositive`、`subStateNegative` 三个效果
- 我们在 `data_loader.py` 中手写了 `refine_card_effects()` 和 `refine_sub_state_effects()` 来做二次精炼
- 这些手写逻辑基于猜测（如用 funcTargetType 区分 subState），而非 Chaldea 的正确逻辑

### Chaldea 的正确做法

Chaldea 的 `effect.dart` 中有 13 个效果带 `validate` 函数，用于精确区分同一 funcType/buffType 的不同语义：

```dart
// 1. 卡色区分 — 靠 buff.ckSelfIndv 中的 Trait
upQuick: validate = (func) => func.buffs.any(
    (buff) => buff.ckSelfIndv.contains(Trait.cardQuick.value));
upArts:  validate = (func) => func.buffs.any(
    (buff) => buff.ckSelfIndv.contains(Trait.cardArts.value));
upBuster: validate = (func) => func.buffs.any(
    (buff) => buff.ckSelfIndv.contains(Trait.cardBuster.value));

// 2. subState 区分 — 靠 func.vals 中的 Trait
subStatePositive: validate = (func) =>
    func.vals.contains(Trait.buffPositiveEffect.value);
subStateNegative: validate = (func) =>
    func.vals.contains(Trait.buffNegativeEffect.value);

// 3. 弱体耐性 vs 通用 — 靠 buff.ckOpIndv 排除特定 Trait
upTolerance: validate = (func) => func.buffs.first.ckOpIndv.every(
    (trait) => ![Trait.buffPositiveEffect, Trait.buffIncreaseDamage].contains(trait));

// 4. 状态付与率 — 靠 buff.ckSelfIndv 中的 Trait
upGrantstatePositive: validate = (func) => func.buffs.any(
    (buff) => buff.ckSelfIndv.contains(Trait.buffPositiveEffect.value));
upGrantstateNegative: validate = (func) => func.buffs.any(
    (buff) => buff.ckSelfIndv.contains(Trait.buffNegativeEffect.value));

// 5. 被强化成功率 — 靠 buff.ckOpIndv 中的 Trait
upReceivePositiveEffect: validate = (func) =>
    func.buffs.first.ckOpIndv.contains(Trait.buffPositiveEffect.value);

// 6. 触发型技能 — 靠 buff.type 是否属于特定集合
triggerFunc: validate = (func) =>
    kBuffValueTriggerTypes.containsKey(func.buffs.first.type);
```

### 关键 Trait 常量

validate 逻辑依赖以下 Trait 值：

| Trait 名 | 数值（需确认） | 用途 |
|:---------|:-------------|:-----|
| `cardQuick` | 4001 | 区分绿卡效果 |
| `cardArts` | 4002 | 区分蓝卡效果 |
| `cardBuster` | 4003 | 区分红卡效果 |
| `buffPositiveEffect` | 需查 | 区分正面/负面状态 |
| `buffNegativeEffect` | 需查 | 区分负面状态 |
| `buffIncreaseDamage` | 需查 | 排除增伤类 buff |

## 方案分析

### 方案 A：将 validate 逻辑编码到 effect_schema.json

**思路**：在 `sync_chaldea.py` 提取时，将 validate 函数的逻辑转换为声明式 JSON 规则，存入 `effect_schema.json`。`data_loader.py` 的效果匹配逻辑读取这些规则执行校验。

**effect_schema.json 扩展示例**：
```json
{
  "name": "upArts",
  "funcTypes": [],
  "buffTypes": ["upCommandall", "upCommandatk", "upCommandstar", "upCommandnp"],
  "validate": {
    "type": "buff_ckSelfIndv_contains",
    "traitValue": 4002
  }
}
```

```json
{
  "name": "subStateNegative",
  "funcTypes": ["subState"],
  "buffTypes": [],
  "validate": {
    "type": "func_vals_contains",
    "traitValue": "<buffNegativeEffect_value>"
  }
}
```

**优点**：
- validate 逻辑自动从 Chaldea 源码提取，不手写
- `data_loader.py` 的匹配逻辑通用化，不再需要 `refine_card_effects` 等特化函数
- Chaldea 更新 effect.dart 时，重新运行 `sync_chaldea.py` 自动同步

**缺点**：
- 需要将 Dart lambda 转换为声明式规则，解析复杂度较高
- validate 模式有限（约 5 种模式），但需要覆盖完整

**改动范围**：
1. `sync_chaldea.py` — 扩展 `parse_effect_schema()` 提取 validate 逻辑 + Trait 常量
2. `effect_schema.json` — 新增 `validate` 字段
3. `data_loader.py` — 重写效果匹配逻辑，实现声明式 validate 执行器，删除 `refine_card_effects` 和 `refine_sub_state_effects`

### 方案 B：在 data_loader.py 中硬编码 Chaldea 的 validate 逻辑

**思路**：直接将 Chaldea 的 13 个 validate 函数翻译为 Python，在 `data_loader.py` 中实现。

**优点**：
- 实现简单直接
- 不需要修改 schema 格式

**缺点**：
- 手动翻译，容易出错
- Chaldea 更新 validate 逻辑时需要手动同步
- 违反 AGENTS.md 的 "Schema Mirror 同步机制" 约束

### 方案 C：混合方案（推荐）

**思路**：
1. **sync_chaldea.py 自动提取**：解析 validate 的 5 种固定模式，生成声明式规则
2. **data_loader.py 通用执行器**：实现 5 种 validate 类型的执行逻辑
3. **未覆盖的 buffType**：不扩展 effect_schema 的覆盖范围（55 种效果足够应对当前查询需求），但确保物理层数据传给 LLM 时有翻译映射

**优点**：
- validate 逻辑自动同步
- 改动范围可控
- 不过度扩展 effect_schema

## 从 Skill 需求反推：知识消费盘点

> 用户原则："先从 Skill 出发，看需要哪些 knowledge；未来做成标准架构：原始数据 → 从者数据模型 → 知识模型 → Skill 需求。后续增量场景都从需求反推。"

### 当前架构数据流

```
原始数据 (Atlas Academy API)
       ↓ data_loader.py
从者数据模型 (servants_db.json)
  ├── 平坦字段：className, rarity, attribute, cards, traits, ...
  ├── 预消化物理层：skills[], noblePhantasms[], appendPassive[]
  └── Materialized Views：skillEffects[], npEffects[], skillDetails[], npCharges[]
       ↓                          ↑
       ↓                   effect_schema.json (知识模型)
       ↓                          ↑
       ↓                   sync_chaldea.py ← chaldea/effect.dart
       ↓
Skill 模块 (server/skills/query/*.py, response/*.py)
```

### Query Skills 知识依赖分析（11 个）

| Skill | 消费的从者模型字段 | 知识层依赖 | 备注 |
|:------|:------------------|:----------|:-----|
| `lookup_servant` | name, aliasCN, originalName | nicknames.json (配置) | 仅名称匹配 |
| `compare_servants` | 同 lookup | nicknames.json (配置) | 仅名称匹配 |
| `search_by_class` | className | 无 | 枚举值直匹配 |
| `search_by_rarity` | rarity | 无 | 数值比较 |
| `search_by_attribute` | attribute | 无 | 枚举值直匹配 |
| `search_by_cards` | cards, npCard, npTarget | 无 | 枚举值直匹配 |
| `search_by_traits` | traits | 无 | Trait ID 直匹配 |
| `search_by_np_charge` | hasNpCharge, totalCharge, npCharges | **Materialized View** (build-time) | data_loader 预计算 |
| `search_by_skill_effect` | skillEffects, skillDetails | **effect_schema.json** + MV | 运行时查 zh→en 映射 |
| `search_by_np_effect` | npEffects | **effect_schema.json** + MV | 运行时查 MV |

### Response Skills 知识依赖分析（4 个）

| Skill | 消费数据 | 知识层依赖 | 备注 |
|:------|:---------|:----------|:-----|
| `respond_servant_detail` | 完整从者 JSON（含物理层） | Prompt 指引 | LLM 自行解读物理层数据 |
| `respond_servant_list` | 从者列表摘要 | 无 | 仅展示平坦字段 |
| `respond_servant_compare` | 多从者 JSON | 无 | 仅对比平坦字段 |
| `respond_support_analysis` | 从者 JSON（含效果） | Prompt 指引 | LLM 分析辅助能力 |

### 关键发现

1. **effect_schema.json 是唯一的领域知识依赖**
   - Build-time：`data_loader.py` 用它生成 Materialized View（skillEffects/npEffects/skillDetails）
   - Runtime：`search_by_skill_effect` 用它做 中文→英文 效果名反查
   - 其他 9 个 Query Skill 完全不依赖任何知识层

2. **validate 逻辑的实际影响范围**
   - Chaldea 中 13 个带 validate 的效果中，**9 个被当前 Skill 实际消费**：
     - 卡色系（upArts, upQuick, upBuster）→ `search_by_skill_effect` / `search_by_np_effect`
     - subState 系（subState, subStatePositive, subStateNegative）→ 同上
     - 状态付与率（upGrantstatePositive, upGrantstateNegative）→ 同上
     - 弱体耐性（upTolerance）→ 同上
   - **2 个未被消费**：
     - `upReceivePositiveEffect`（被强化成功率）— schema 中有定义但查询频率极低
     - `triggerFunc`（触发型技能）— schema 中有定义但查询频率极低
   - 结论：**9 个高频 validate 效果需要优先正确实现**

3. **Response Skill 的隐性风险**
   - `respond_servant_detail` 和 `respond_support_analysis` 将完整从者 JSON（含物理层 skills/noblePhantasms）传给 LLM
   - 物理层包含大量未映射的英文枚举值（68 种 buffType），LLM 自行翻译会产生幻觉
   - 但这属于"预消化"问题（AGENTS.md 准则 1），不属于 validate 问题

4. **手写 validate vs 自动同步**
   - 当前 `data_loader.py` 中有两个手写 refine 函数：
     - `refine_card_effects()` — 用 buff name 猜测卡色，不够精确
     - `refine_sub_state_effects()` — 用 funcTargetType 区分，与 Chaldea 逻辑不一致
   - Chaldea 的正确做法是用 **Trait 值**（`buff.ckSelfIndv`/`func.vals`）区分

### 需求反推结论

基于盘点，当前 Skill 层的知识需求可以精确表述为：

```
当前 Skill 需要的知识 = effect_schema.json 中 55 种效果的准确映射
                     = funcType/buffType 映射 + 9 个 validate 规则的正确实现
```

**不需要**：
- 扩展 68 种未映射 buffType（当前 Skill 不消费）
- 新增效果分类（当前 55 种已覆盖所有 Query Skill 的需求）

**需要**：
- 修复 9 个 validate 规则（从 Chaldea 自动同步，替换手写 refine 函数）
- 确保 `sync_chaldea.py` 能提取 validate 逻辑 + Trait 常量值
- `data_loader.py` 中的效果匹配引擎支持声明式 validate 执行

## 关于未映射的 68 种 buffType

这是一个独立问题。当前 55 种效果分类是 Chaldea 项目自身为从者筛选定义的（`SkillEffect.values`），覆盖了最重要的效果。68 种未映射的 buffType 中：

- **部分是 down 系**（`downAtk`, `downDefence` 等）— 与 up 系对应的 debuff 版本，Chaldea 自身也未将它们纳入筛选效果
- **部分是控制系**（`donotAct`, `donotNoble` 等）— 眩晕、宝具封印等
- **部分是战斗机制**（`multiattack`, `overwriteClassRelation` 等）— 连续攻击、职阶相性覆写

**建议**：短期内不扩展，保持与 Chaldea 的 `SkillEffect.values` 列表一致。如果未来需要支持 "有眩晕技能的从者" 这类查询，再按需扩展。

## 待讨论（需求反推后精简）

基于盘点结论，原有 4 个问题可以精简为 2 个核心决策：

### 决策 1：validate 逻辑的实现方式

**背景**：当前 `data_loader.py` 中有 2 个手写 refine 函数，它们基于猜测而非 Chaldea 的正确逻辑。需要修复 9 个被 Skill 实际消费的 validate 效果。

**选项**：
- **A. 声明式同步**（推荐）：`sync_chaldea.py` 将 validate 逻辑提取为 JSON 规则存入 `effect_schema.json`，`data_loader.py` 实现通用 validate 执行器。优点：自动同步，Chaldea 更新后重新运行即可。
- **B. 手动翻译**：直接将 13 个 validate 函数翻译为 Python 写死在 `data_loader.py`。优点：简单直接。缺点：违反 Schema Mirror 原则，后续维护成本高。

### 决策 2：中文翻译源

**背景**：当前 `aliases_zh` 是手写的，可能与 Chaldea 的多语言翻译不一致。

**选项**：
- **A. 保持手写**：当前 55 种效果的中文翻译已经够用，手动维护。
- **B. 从 Chaldea 自动提取**：`sync_chaldea.py` 同时提取 Chaldea 的中文/日文翻译映射。优点：翻译准确一致。缺点：需要解析 Chaldea 的多语言资源文件。

### 已不需要讨论的问题

- ~~68 种未映射 buffType 是否处理？~~ → **不处理**。盘点确认当前 Skill 不消费这些 buffType，按需求反推原则，等到新 Skill 需要时再扩展。
- ~~validate 规则的 5 种模式是否足够？~~ → **足够**。13 个 validate 效果只用了 5 种模式，全部可用声明式规则表达。

---

## 议题 3：knowledge 层架构重新定位（2026-05-09 讨论）

### 背景

`server/knowledge/` 下有 7 个文件，但盘点发现大部分文件在 runtime 没有代码消费。需要讨论：
1. 每个文件的处理方式
2. knowledge 层的定位标准
3. 烘焙 vs 查表的判定规则

### knowledge 文件消费盘点

| 文件 | 大小 | 生产者 | Runtime 消费 | Build-time 消费 | 状态 |
|:-----|:-----|:-------|:------------|:---------------|:-----|
| `effect_schema.json` | 13KB | sync_chaldea.py | main.py (翻译), search_by_skill_effect (zh→en 反查) | data_loader.py (生成 MV) | **保留** |
| `mappings.json` | 162KB | sync_chaldea.py | 无 | data_loader.py (svt_names) | **保留** |
| `class_mapping.json` | 8KB | sync_chaldea.py | main.py (启动校验) | 无 | **保留** |
| `func_types.json` | 8KB | sync_chaldea.py | 无 | 无 | **移到 docs/reference/** |
| `buff_types.json` | 16KB | sync_chaldea.py | 无 | 无 | **移到 docs/reference/** |
| `func_target_types.json` | 2KB | sync_chaldea.py | 无 | 无 | **移到 docs/reference/** |
| `_meta.json` | 0.3KB | sync_chaldea.py | 无 | 无 | **保留**（版本追踪） |

### 已达成决策

#### 决策 3.1：各文件处理方式

| 文件 | 决策 | 理由 |
|:-----|:-----|:-----|
| `func_types.json` / `buff_types.json` / `func_target_types.json` | **移到 `docs/reference/`** | 纯人类参考文档，无代码消费 |
| `class_mapping.json` | **保持现状** | 启动校验有价值（部署后发现问题比 build 时更直观） |
| `effect_schema.json` | **保留，runtime 仍需读取** | 中文→英文查询映射是筛选逻辑的一部分 |
| `mappings.json` | **保留** | 纯 build-time 消费（生成 aliasCN） |
| `_meta.json` | **保留** | sync 版本追踪 |

#### 决策 3.2：knowledge 层定位标准

> `server/knowledge/` = `sync_chaldea.py` 从 Chaldea Dart 源码提取的领域知识

- **主要是 build-time 消费**（由 `data_loader.py` 生成 Materialized View）
- **允许 runtime 读取**，但仅限于「查询输入映射」场景（如中文→英文效果名反查）
- 文件来源必须是 `sync_chaldea.py` 的自动化输出，禁止手工编辑

#### 决策 3.3：烘焙 vs 查表 判定标准

| 场景 | 策略 | 存储位置 | 示例 |
|:-----|:-----|:--------|:-----|
| Skill filter 筛选匹配 | **烘焙到 MV** | `servants_db.json` | `skillEffects: ["upAtk"]` |
| 查询输入映射（用户中文 → 英文 key） | **Runtime 查表** | `server/knowledge/` | "攻击力提升" → "upAtk" |
| 展示翻译（英文 → 中文 label） | **Runtime 查表** | `server/knowledge/` | "upAtk" → "攻击力提升" |

**判定口诀：筛选字段烘焙，映射翻译查表**

- **烘焙**：知识被 QuerySkill 的 `filter()` 用于 `==` / `in` / `contains` 判断
- **查表**：知识仅用于查询输入解析或展示翻译，且为多实体共用的映射关系（避免冗余膨胀）

> 实际膨胀评估：55 种效果翻译如果烘焙到 500+ 从者的 MV 中，约增加 70KB（占 42MB 的 0.17%），但作为架构标准，未来特性翻译（~15KB，嵌套多处）和素材翻译（~10KB，嵌套深）的膨胀会更显著，因此统一采用查表策略。

---

## 议题 4：语义匹配增强（2026-05-09 讨论）

### 背景

用户查询不可能百分百匹配效果名称。例如用户可能说"攻击提升"而非"攻击力提升"，或说"能挡伤害的从者"而期望匹配到无敌/回避/毅力。LLM 的语义理解能力应被充分利用。

### 已达成决策

#### 决策 4.1：双层语义匹配策略

| 层次 | 机制 | 实现位置 | 作用 |
|:-----|:-----|:--------|:-----|
| **LLM 路由层** | Prompt 注入效果语义描述 | `server/prompts.py` | LLM 在路由阶段将自然语言映射到正确的效果 key |
| **Skill 执行层** | 子串模糊 fallback | `search_by_skill_effect.py` / `search_by_np_effect.py` | 精确查表 miss 后做子串匹配兜底 |

#### 决策 4.2：效果语义描述

- 在 `effect_schema.json` 中为每个效果新增 `description` 字段（55 个效果全覆盖）
- 描述由 `sync_chaldea.py` 的 `EFFECT_DESCRIPTIONS` 字典维护，同步时写入 JSON
- 路由 Prompt 动态加载描述并注入，格式：`effectName: 中文名 — 语义描述`
- 估算约 800 token，在可接受范围内

#### 决策 4.3：模糊匹配策略

- `_resolve_effect_name()` 精确查表 miss 后，做**子串匹配**兜底
- 匹配逻辑：`name in alias` 或 `alias in name`，取首个匹配
- 不引入外部向量数据库或 embedding 模型，保持轻量
- 未来可按需升级为 fuzzy matching（如 Levenshtein 距离）

### Token 成本评估

| 项目 | 增量 | 备注 |
|:-----|:-----|:-----|
| Prompt 效果描述段 | ~800 token | 每次路由请求增加 |
| effect_schema.json | ~3KB | 仅影响文件大小，不影响 runtime |

对于路由请求（通常 ~2000 token），增加 800 token（~40%）是可接受的。效果描述段的价值在于显著减少 Stage 2 参数精填的需求，因为 LLM 在 Stage 1 就能准确映射效果名。
