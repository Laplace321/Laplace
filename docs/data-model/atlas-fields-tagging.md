# Atlas Academy 字段打标表

> 请在「你的决策」列填写：✅ 保留 / ❌ 不保留
>
> 未来如需新增字段，直接提需求加回即可（Atlas API 数据始终完整，`data_loader.py` 加一个字段只需一行代码）。
>
> 打标完成后，`data_loader.py` 将据此重构物理层数据模型。

## 架构概览

```
Atlas Academy API (150MB full export)
       ↓  data_loader.py (ETL)
Physical Layer (servants_db.json)
  ├── 核心扁平字段（直接可查）
  ├── 嵌套原始数据（skills/NP/passives，视图函数的源）
  └── 丢弃的字段（语音、立绘、成长曲线等）
       ↓
Materialized Views (预计算，持久化写入 JSON)
  ├── npCharges[]               充能三分类
  ├── skillEffects[] / npEffects[]  效果扁平化
  ├── cards / npCard / npTarget  配卡语义化
  └── (未来按需扩展)
       ↓
Virtual Views (实时计算，视图函数，不持久化)
  ├── view_ascension_materials(s)
  ├── view_skill_materials(s)
  └── (未来按需扩展)
       ↓
Skill Layer — skill.filter(servant, params)
```

## 体积参考

| 策略 | 单从者体积 | 456 从者总量 |
|:-----|:----------|:------------|
| 当前 Laplace（预消化扁平） | ~1.7 KB | 784 KB |
| 推荐保留（扁平 + skills/NP 嵌套） | ~25 KB | ~11 MB |
| 全量保留（含 extraPassive） | ~120 KB | ~53 MB |

---

## 字段打标表

### 1. 身份标识

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 1 | `id` | int | 6 B | ✅ 保留 | 主键 |
| 2 | `collectionNo` | int | 3 B | ✅ 保留 | 图鉴编号，排序用 |
| 3 | `name` | str | ~15 B | ✅ 保留 | 英文名 |
| 4 | `originalName` | str | ~13 B | ✅ 保留 | 日文原名 |
| 5 | `ruby` | str | ~13 B | ❌ 不保留 | 假名注音，无查询价值 |
| 6 | `battleName` | str | ~8 B | ❌ 不保留 | 战斗中简称（如"Altria"） |
| 7 | `originalBattleName` | str | ~7 B | ❌ 不保留 | 日文战斗简称 |

### 2. 分类

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 8 | `classId` | int | 1 B | ❌ 不保留 | 有 className 足够 |
| 9 | `className` | str | ~8 B | ✅ 保留 | 职阶（saber/caster 等） |
| 10 | `type` | str | ~8 B | ✅ 保留 | normal/heroine/enemy，区分可召唤从者 |
| 11 | `flag` | str | ~8 B | ❌ 不保留 | 内部标记，无业务价值 |
| 12 | `flags` | list | 2 B | ❌ 不保留 | 通常为空 |
| 13 | `rarity` | int | 1 B | ✅ 保留 | 星级 |
| 14 | `cost` | int | 2 B | ✅ 保留 | 编队 cost，低频查询 |

### 3. 面板数值

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 15 | `atkBase` | int | 4 B | ❌ 不保留 | 基础 ATK |
| 16 | `atkMax` | int | 5 B | ✅ 保留 | 满级 ATK，队伍构建参考 |
| 17 | `hpBase` | int | 4 B | ❌ 不保留 | 基础 HP |
| 18 | `hpMax` | int | 5 B | ✅ 保留 | 满级 HP |
| 19 | `lvMax` | int | 2 B | ❌ 不保留 | 可从 rarity 推算 |

### 4. 战斗属性

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 20 | `gender` | str | ~8 B | ✅ 保留 | 性别（特攻条件） |
| 21 | `attribute` | str | ~6 B | ✅ 保留 | 天地人星兽（属性相克） |
| 22 | `traits` | list | ~525 B | ✅ 保留 | 特性标签（特攻/加成条件核心） |
| 23 | `starAbsorb` | int | 2 B | ✅ 保留 | 集星权重，暴击队构建核心 |
| 24 | `starGen` | int | 3 B | ❌ 不保留 | 暴击星生成率 |
| 25 | `instantDeathChance` | int | 3 B | ✅ 保留 | 即死率，极低频 |

### 5. 指令卡

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 26 | `cards` | list[5] | 25 B | ✅ 保留 | 配卡组合（1=A, 2=B, 3=Q） |
| 27 | `hitsDistribution` | dict | 83 B | ✅ 保留 | 每卡 Hit 数分布，NP 回收计算需要 |
| 28 | `cardDetails` | dict | 511 B | ❌ 不保留 | 每卡详细参数（含补正值） |

### 6. 技能（嵌套结构 — 体积大户）

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 29 | `skills` | list[3] | **~18.3→~3.9 KB** | ✅ 保留(预消化) | 裁剪顶层/function/buff 元数据，svals 只保留满级 |
| 30 | `classPassive` | list[3] | **~5.6 KB** | ✅ 保留原始 | 职阶被动（阵营特攻、Arts 补正等） |
| 31 | `extraPassive` | list[26] | **~49.3 KB** | ❌ 不保留 | 活动限定被动，单从者占 40%+ 体积 |
| 32 | `appendPassive` | list[5] | **~12.4→~0.5 KB** | ✅ 保留(预消化) | 只保留满级数值+解锁素材，丢弃 svals[0-8] 和 functions 嵌套 |

### 7. 宝具（嵌套结构）

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 33 | `noblePhantasms` | list | **~11.8→~8.3 KB** | ✅ 保留(预消化) | 裁剪顶层/buff 元数据，保留全部 OC svals |

### 8. 素材

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 34 | `ascensionMaterials` | dict | **~4.6 KB** | ✅ 保留 | 灵基再临素材 |
| 35 | `skillMaterials` | dict | **~7.2 KB** | ✅ 保留 | 技能升级素材 |
| 36 | `appendSkillMaterials` | dict | **~7.3 KB** | ✅ 保留 | 追加技能素材 |
| 37 | `costumeMaterials` | dict | 2 B | ✅ 保留 | 灵衣素材 |

### 9. 资源与展示

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 38 | `extraAssets` | dict | **~3.1 KB** | ✅ 保留(仅 faceUrl) | 只提取头像 URL，丢弃立绘/战斗贴图 |
| 39 | `coin` | dict | 444 B | ❌ 不保留 | 从者硬币 |

### 10. 成长曲线

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 40 | `growthCurve` | int | 2 B | ❌ 不保留 | 成长类型编号 |
| 41 | `atkGrowth` | list[120] | 766 B | ❌ 不保留 | 每级 ATK 值 |
| 42 | `hpGrowth` | list[120] | 788 B | ❌ 不保留 | 每级 HP 值 |
| 43 | `expGrowth` | list[120] | 1.0 KB | ❌ 不保留 | 升级所需经验 |
| 44 | `expFeed` | list[120] | 840 B | ❌ 不保留 | 吃卡获得经验 |

### 11. 绊 / 情人节

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 45 | `bondGrowth` | list | 121 B | ❌ 不保留 | 绊等级经验需求 |
| 46 | `bondGifts` | dict | 1.6 KB | ❌ 不保留 | 绊礼物 |
| 47 | `bondEquip` | int | 7 B | ❌ 不保留 | 绊礼装 ID |
| 48 | `bondEquips` | list | 9 B | ❌ 不保留 | 绊礼装 ID 列表 |
| 49 | `valentineEquip` | list | 9 B | ❌ 不保留 | 情人节礼装 |
| 50 | `valentineScript` | list | 135 B | ❌ 不保留 | 情人节剧情脚本 |

### 12. 灵基变化

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 51 | `limits` | list | 1.4 KB | ❌ 不保留 | 各灵基阶段数值 |
| 52 | `ascensionAdd` | dict | 1.9 KB | ❌ 不保留 | 灵基追加属性（灵基后改名等） |
| 53 | `traitAdd` | list | 1.2 KB | ❌ 不保留 | 灵基追加特性 |
| 54 | `svtChange` | list | 2 B | ❌ 不保留 | 灵基变化数据 |
| 55 | `ascensionImage` | list | 2 B | ❌ 不保留 | 灵基再临图片 |
| 56 | `overwrites` | list | 2 B | ❌ 不保留 | 覆写数据 |

### 13. 其他

| # | 字段 | 类型 | 单从者体积 | 决策 | 说明 |
|:--|:-----|:-----|:----------|:-----|:-----|
| 57 | `script` | dict | 2 B | ❌ 不保留 | 内部脚本数据 |
| 58 | `charaScripts` | list | 1.1 KB | ❌ 不保留 | 角色脚本（战斗演出） |
| 59 | `battlePoints` | list | 2 B | ❌ 不保留 | 战斗点数 |
| 60 | `relateQuestIds` | list | 2 B | ❌ 不保留 | 关联关卡 |
| 61 | `trialQuestIds` | list | 10 B | ❌ 不保留 | 体验关卡 |
| 62 | `profile` | dict | ~2 KB | ❌ 不保留 | 人物传记 / 语音（部分从者极大） |

---

## Materialized Views（预计算视图，已实现）

以下字段由 `data_loader.py` 从原始嵌套数据预计算后写入 `servants_db.json`：

| 视图字段 | 源数据 | 说明 |
|:---------|:-------|:-----|
| `npCharges[]` | skills → functions(gainNp) | 充能详情（来源/目标/数值） |
| `maxSelfCharge` | npCharges | 最大自充值 |
| `maxPtOneCharge` | npCharges | 最大单体他充值 |
| `maxPtAllCharge` | npCharges | 最大群充值 |
| `totalCharge` | npCharges | 总充能 |
| `hasNpCharge` | npCharges | 是否有充能 |
| `skillEffects[]` | skills → functions → buffs | 技能效果扁平化 |
| `npEffects[]` | noblePhantasms → functions → buffs | 宝具效果扁平化 |
| `skillDetails[]` | skills | 技能名称+CD 摘要 |
| `npCard` | noblePhantasms → card | 宝具颜色 |
| `npTarget` | noblePhantasms → funcTargetType | 宝具范围（单体/全体/辅助） |
| `cards` | cards (语义化) | 配卡组合（如 {arts:3, buster:1, quick:1}） |
| `faceUrl` | extraAssets → faces | 头像 URL |
| `aliasCN` | 外部配置 | 中文别名 |

## Virtual Views（实时计算视图，待实现）

以下视图在 Skill 查询时实时计算，不写入 JSON：

| 视图函数 | 源数据 | 适用场景 |
|:---------|:-------|:---------|
| `view_ascension_materials(s)` | ascensionMaterials | "练某某需要什么素材" |
| `view_skill_materials(s)` | skillMaterials | "技能升级需要什么" |
| `view_np_gain_stats(s)` | hitsDistribution + cardDetails | "NP 回收效率排行" |
| `view_append_passives(s)` | appendPassive | "追加技能充能量" |

---

## 实施记录

- **打标完成**: 2026-05-08
- **ETL 重构完成**: 2026-05-08，`data_loader.py` 已按打标结果新增物理层字段
- **`fetch_servants()` 重命名为 `fetch_normal_servants()`**：语义更明确，未来如需非 normal 从者再新增函数
- **`appendPassive` 预消化**：只保留满级数值 + 解锁素材（12.4 KB → 3.3 KB/从者）
- **skills 预消化**: 裁剪 skill 顶层（保留 id/num/name/type/coolDown/functions）+ function（保留 funcType/funcTargetType/buffs/svals 满级）+ buff（保留 type/name/vals/tvals），18.3 KB → 3.9 KB/从者（-79%）
- **noblePhantasms 预消化**: 裁剪 NP 顶层（保留 id/num/name/card/type/rank/npGain/individuality/functions）+ buff（同上），保留全部 OC svals（svals/svals2-5），11.8 KB → 8.3 KB/从者（-30%）
- **`servants_db.json` 体积**: 42 MB（indent=2），纯数据 18.9 MB（从 59 MB / 26.8 MB 优化至此）
- **`servants_db.json` 从 git 移除**: 加入 .gitignore，首次运行需执行 `python3 -m server.data_loader`
