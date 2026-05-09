# ADR-019: Knowledge 层定位与 Validate 同步机制

- **日期**: 2026-05-09
- **状态**: 已决策 (Decided)
- **参与者**: 用户（羽殊）、AI Agent
- **前置讨论**: [Effect Validate 逻辑自动同步](../architecture-discussions/effect-validate-sync.md)
- **触发问题**: validate 逻辑缺失导致效果映射错误（千子村正宝具误标注）

---

## 一、问题背景

### 1.1 validate 逻辑缺失

`sync_chaldea.py` 从 Chaldea `effect.dart` 提取 SkillEffect 定义时，丢弃了 `validate` 函数，导致：
- 同一 funcType/buffType 的不同语义效果无法区分
- `data_loader.py` 中手写了 `refine_card_effects()` 和 `refine_sub_state_effects()` 做猜测性修正
- 千子村正的宝具效果被错误标注为 `subStateNegative`

### 1.2 knowledge 层定位不清

`server/knowledge/` 下有 7 个文件，但其中 3 个（func_types.json, buff_types.json, func_target_types.json）在 runtime 和 build-time 均无代码消费，属于纯参考文档。

---

## 二、决策内容

### 决策 1：validate 声明式同步

**选定方案**：`sync_chaldea.py` 将 Dart validate lambda 转换为 5 种声明式 JSON 规则，存入 `effect_schema.json`。`data_loader.py` 实现通用 `apply_validate()` 执行器。

**5 种规则类型**：

| 类型 | 含义 | 示例效果 |
|:-----|:-----|:--------|
| `buff_ckSelfIndv_contains` | buff.ckSelfIndv 包含指定 Trait | upArts, upQuick, upBuster |
| `buff_ckOpIndv_contains` | buff.ckOpIndv 包含指定 Trait | upReceivePositiveEffect |
| `buff_ckOpIndv_every_not_in` | buff.ckOpIndv 全部不在指定集合 | upTolerance, avoidStateNegative |
| `func_vals_contains` | func.vals 包含指定 Trait | subStatePositive, subStateNegative |
| `buff_type_in_trigger_set` | buff.type 属于 triggerBuffTypes | triggerFunc |

**理由**：
- 自动同步，Chaldea 更新后重新运行 `sync_chaldea.py` 即可
- 符合 Schema Mirror 同步机制（AGENTS.md 准则 3）
- 消除手写 refine 函数的猜测性风险

### 决策 2：中文翻译自动提取

**选定方案**：从 Chaldea Data 的 `enums.json` 自动拉取翻译，按 `effect.dart` 的 `transl` getter 优先级（effect_type → buff_type → func_type）解析，追加玩家常用俗称（`PLAYER_SLANG_ZH`）。

**理由**：
- 翻译与 Chaldea 保持一致，避免手写维护的偏差
- 玩家俗称（如"自充""爆伤""集星"）单独维护，不受 Chaldea 更新影响

### 决策 3：knowledge 层定位标准

**定位**：`server/knowledge/` = `sync_chaldea.py` 从 Chaldea Dart 源码提取的领域知识。

**规则**：
- 主要是 build-time 消费（由 `data_loader.py` 生成 Materialized View）
- 允许 runtime 读取，但仅限「查询输入映射」场景
- 文件来源必须是 `sync_chaldea.py` 的自动化输出，禁止手工编辑
- 无代码消费的纯参考文件移到 `docs/reference/`

**烘焙 vs 查表判定口诀**：筛选字段烘焙，映射翻译查表。

| 场景 | 策略 | 存储位置 |
|:-----|:-----|:--------|
| Skill filter 筛选匹配 | 烘焙到 MV | `servants_db.json` |
| 查询输入映射（中文 → 英文 key） | Runtime 查表 | `server/knowledge/` |
| 展示翻译（英文 → 中文 label） | Runtime 查表 | `server/knowledge/` |

### 决策 4：语义匹配增强

**选定方案**：双层语义匹配。

| 层次 | 机制 | 实现位置 |
|:-----|:-----|:--------|
| LLM 路由层 | Prompt 注入效果语义描述 | `server/prompts.py` |
| Skill 执行层 | 子串模糊 fallback | `search_by_skill_effect.py` |

---

## 三、改动清单

| 文件 | 改动 |
|:-----|:-----|
| `server/sync_chaldea.py` | 新增 validate 规则提取 + Trait 常量提取 + 翻译自动拉取 |
| `server/knowledge/effect_schema.json` | 新增 `validate`, `traits`, `triggerBuffTypes`, `description` 字段 |
| `server/data_loader.py` | 新增 `apply_validate()`, 重写效果匹配逻辑, 删除 refine 函数 |
| `server/prompts.py` | 路由 Prompt 动态注入效果语义描述 |
| `server/skills/query/search_by_skill_effect.py` | 子串模糊 fallback |
| `server/skills/query/search_by_np_effect.py` | 复用 `_resolve_effect_name` |
| 3 个 knowledge 文件 | `git mv` 到 `docs/reference/` |

---

## 四、验证

- 千子村正宝具效果不再包含 `subStateNegative`
- 55/55 个效果有中文别名
- 11 个效果含 validate 规则
- 三步验证全部通过（ruff check + ruff format + pytest）
