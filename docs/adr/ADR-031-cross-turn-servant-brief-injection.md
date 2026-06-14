# ADR-031: 跨轮从者基础数据注入 Pipeline C（A→C 上下文增强）

**状态**: 已实施
**日期**: 2026-06-12
**决策者**: Laplace
**关联 ADR**: ADR-026（多轮对话状态机）、ADR-028（声明式 Pipeline）

---

## 一、背景

trace `b69a2c90` 暴露 Pipeline C（攻略检索）的多轮上下文断链问题：用户先在 Pipeline A 查询「水C呆」，再续问「这个从者在狂阶戴冠战的攻略」，C 链路完全没有把上一轮命中的从者实体注入下游。

第一版修复（commit `131ff76`）仅注入了**从者名字**，让 LLM 把指代词锚定到该实体。但仅有名字仍然不够：

1. **依赖 LLM 训练知识猜测属性**：如果攻略文档没明确提到该从者，LLM 只能模糊回答或猜错职阶 / 卡色 / 宝具类型
2. **新增从者完全失败**：训练 cutoff 后的从者 LLM 不认识，会触发幻觉
3. **攻略推荐合规性差**：戴冠战的职阶强制限制需要交叉验证从者真实职阶；只有名字时 LLM 没有数据依据

Laplace 提出：把上一轮查询到的**完整基础数据**带入下一轮的攻略 prompt，让攻略 LLM 基于真实数据做精准分析。

## 二、决策

### 2.1 数据载体：预消化摘要字符串（方案 B）

将上一轮命中的从者完整数据，通过 `build_servant_brief()` 预消化为中文化、结构化的 Markdown 摘要文本，存入 `TurnSnapshot.servant_briefs: list[str]`。

**对比候选方案**：

| 方案 | 描述 | 选择原因 |
|:--|:--|:--|
| A. 存原始 dict | 在 servants 字段加 `core_data` | ❌ 体积大（10K+），违反预消化纪律 |
| **B. 预消化摘要字符串** | **runtime 翻译 + 截断 + 字段提取** | ✅ 与「数据后端预消化」纪律对齐，体积可控 |
| C. 仅存 collectionNo | 下一轮重新查 servants_db | ❌ 引入跨模块耦合（guide_node → data_loader），多一次 IO |

### 2.2 触发阈值：servants ≤ 3

仅当本轮命中数 ≤ 3 时为每位从者生成 brief。超过则视为列表场景（如「五星弓阶」），仅依赖现有的 `servants` 名字字段做下一轮锚定，避免 token 失控。

### 2.3 单条 brief 上限：1500 字符

`build_servant_brief()` 输出硬截断至 1500 字符，超过用 `…` 结尾。3 个 brief 总长上限 4500 字符（≈ 1500 token），叠加 guide_chunks 后 prompt 总长仍可控。

### 2.4 透传策略：C 链路天然透传 1 轮

C 链路 `guide_node` 不调用 `save_turn`，意味着多轮 C 续问时 SessionStore 中的 turn snapshot 不会被覆盖。因此 A→C→C 三连问场景下，第 3 轮天然能读到第 1 轮 A 写入的 servant_briefs，**无需额外代码**。

### 2.5 Phase 范围：仅 A → C

本期只实现 A 链路写 briefs / C 链路读 briefs。后续如需要可扩展：

| Phase | 范围 | 状态 |
|:--|:--|:--|
| **Phase 1（本 ADR）** | A→C 注入从者数据 | ✅ 已实施 |
| Phase 2-α | B→C 注入 Atlas 实体（活动 / 关卡 / Boss / 礼装） | 暂缓 |
| Phase 2-β | C→C 长对话历史摘要拼接 | 暂缓 |

## 三、实现要点

### 3.1 数据流向

```
[Turn 1: A 链路]
  generate_node → _compute_servant_briefs(state, returned_servants)
                → 仅 pipeline=A + servants ≤ 3 时调用 build_servant_brief()
                → TurnSnapshot.servant_briefs = [brief1, brief2, ...]
                → session_store.save_turn(snapshot)

[Turn 2: C 链路（MINOR / CORRECTION）]
  guide_node → _extract_prev_servant_context(state)
            → (effective_query, prev_servant_label, prev_servant_briefs)
            → _build_guide_generation_prompt(..., prev_servant_briefs=[...])
            → 在 prompt 中注入「从者基础数据（权威结构化数据）」段
```

### 3.2 brief 输出格式（示例）

```markdown
### 玉藻前（Tamamo-no-Mae）
- 职阶：术阶 | 稀有度：5★ | 配卡：{'arts':3,'buster':1,'quick':1} | 宝具卡色：蓝卡 | 宝具目标：辅助 | 总充能：0
- 特性：神性、人型、被EA特攻、天地从者、神灵、兽科从者...
- 技能详情：
  - 变化 A：防御力提升(30%,自身,1T)；防御力提升(30%,自身,3T)
  - 狐之婚嫁 EX：Arts提升(50%,单体（含自身）,3T)；HP回复(2500,单体（含自身）)
  - 咒层·广日照 A：宝具威力提升(30%,队友,3T)
- 宝具：水天日光天照八野镇石
  - 技能冷却减少(1,全队)
  - HP回复(2000,全队) (NP1:2000→NP5:3000)
  - NP增加(25%,全队) (OC1:25%→OC5:50%)
```

### 3.3 prompt 注入段

`_build_guide_generation_prompt` 在 `## 上下文 - 从者基础数据（来自上一轮查询，权威结构化数据）` 段下指引 LLM：

> 下方为该从者的真实属性、技能与宝具。回答前必须先核对：
> - 职阶 / 卡色 / 宝具类型 与攻略要求是否吻合（如戴冠战职阶限制）
> - 技能效果 / 宝具效果 是否能满足攻略中描述的角色定位
> - 若该从者并不适合攻略场景（如职阶不匹配），必须明确指出并给出替代建议

### 3.4 trace 字段扩展

`guide_search` phase 新增 `prev_servant_briefs_count`，便于 BI 监控注入命中率。

## 四、验证

### 4.1 单元测试

- `tests/test_servant_brief.py`：5 个用例覆盖 brief 字段中文化 / 1500 字符截断 / 边界输入
- `tests/test_compute_servant_briefs.py`：7 个用例覆盖 pipeline 触发条件 / servants 数量阈值
- `tests/test_guide_prev_turn_context.py`：扩展 5 个用例覆盖 briefs 透传 / prompt 注入

### 4.2 端到端 trace（`ba439f4d`）

```
Turn 1: 「水C呆」
  → A 链路 → 命中 1 位 → save_turn(servant_briefs=[brief])

Turn 2: 「那这个角色在狂阶戴冠战如何组队」
  → MINOR → guide_search:
     query: "Altria Caster 那这个角色在狂阶戴冠战如何组队"
     prev_servant: "Altria Caster"
     prev_servant_briefs_count: 1
  → LLM 回复明确引用真实属性：「她本身是狂阶、蓝卡单体宝具、70%自充」
  → 攻略推荐合规：「完全符合狂阶戴冠战上场限制和蓝卡爆发需求」
```

## 五、影响

### 5.1 token 成本

C 链路 prompt 增加约 +500~1500 token / 请求（≈ +20% 成本），换取攻略推荐的数据准确性。

### 5.2 后续待办

- `TurnSnapshot.servants[0].name` 仍是英文（如 `Altria Caster`），未做中文化预消化。本 ADR 不修复，后续可在 servants 字段层做 `aliasCN` 透传
- B→C / C→C 多轮场景的扩展按需启动（参见 §2.5）

### 5.3 不影响项

- A 链路本身行为不变
- C 链路 MAJOR / 无 prev_turn 场景与原行为一致
- 数据陈旧风险可接受（多轮对话上下文一致比"最新"更重要）
