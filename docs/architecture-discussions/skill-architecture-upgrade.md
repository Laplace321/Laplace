# Skill-Based Architecture 升级讨论

> 参考文献：[Designing, Refining, and Maintaining Agent Skills at Perplexity](https://research.perplexity.ai/articles/designing-refining-and-maintaining-agent-skills-at-perplexity)

## 背景

Laplace 在 ADR-018 中引入了 Skill-Based Architecture，将查询逻辑拆分为独立的 Skill 模块（`@register_skill` 注册），通过 LLM 路由层将自然语言映射到 Skill 调用组合。当前架构已稳定运行，支撑 15 个 Skill（11 个 Query + 4 个 Response）。

Perplexity 于 2025 年公开了其 Agent Skills 的设计、打磨与维护指南。尽管 Perplexity 的 Skill 是"给 LLM 注入的上下文文档"（SKILL.md），与我们的"后端执行型 Python 模块"本质不同，但其中多个设计哲学具有普适价值。

本文基于该文献，对照 Laplace 现状，识别出 5 个可落地的优化方向。

---

## 现状量化

| 指标 | 当前值 |
|:---|:---|
| 已注册 Skill 总数 | 15（11 Query + 4 Response） |
| 路由 Prompt 字符数 | **7,676 字符**（~4,000 token） |
| 其中：效果 hints 行数 | **57 行 / 2,705 字符**（每次全量注入） |
| 其中：few-shot 示例数 | **12 个** |
| 其中：路由规则数 | **10 条** |
| 生成 Prompt 字符数 | 2,337 字符（~2,300 token） |
| 路由 + 生成 总固定成本 | **~6,300 token / 每次对话** |

---

## 优化方向一：路由 Prompt 分层减肥（P1）

### Perplexity 的核心洞察

> "索引层每个 token 都要斤斤计较，因为每个 session、每个用户都在为它付费。"

Perplexity 将 Skill 的上下文成本分为三层：

| 层级 | 预算 | 何时付费 |
|:---|:---|:---|
| Index（索引层） | ~100 token/Skill | **每次对话永远在付** |
| Load（加载层） | ~5,000 token | Skill 被加载时一次性付 |
| Runtime（运行时层） | 不限 | Agent 真去读了才付 |

### 我们的问题

当前路由 Prompt 是**单层全量注入**——所有信息（Skill 列表、效果语义表、规则、示例）每次对话都完整注入。~4,000 token 的路由 Prompt 中：

- **效果 hints（57 行 / 2,705 字符）**：占路由 Prompt 的 35%，但只有效果类查询才需要
- **few-shot 示例（12 个）**：占约 25%，但很多场景用不到（如量化筛选示例只在"超过50%"类查询时有用）
- **路由规则 9（OR 禁令）和规则 10（量化参数）**：只在特定效果查询场景才需要

### 方案对比

| 方案 | 描述 | Token 节省 | 复杂度 | 风险 |
|:---|:---|:---|:---|:---|
| **A. 效果 hints 按需注入** | 路由分两步：先判断是否涉及效果 → 是则注入效果表 | ~35%（~1,400 token） | 中（需两步路由或启发式预判） | 预判可能误判 |
| **B. 效果 hints 压缩** | 只保留 effectName + 首个中文别名，去掉 description | ~15%（~600 token） | 低 | 路由精度可能下降 |
| **C. few-shot 精简** | 从 12 个减到 5 个核心示例 | ~15%（~600 token） | 低 | 需验证覆盖率 |
| **D. B + C 组合** | 压缩 hints + 精简示例 | ~30%（~1,200 token） | 低 | 综合 |

### 讨论结论

**选择方案 A（启发式按需注入）**。

关键决策依据：项目后续会接入大量新 Skill，且可能作为开源项目，需要从一开始将架构可扩展性做到最优。方案 D 的"压缩"只是延缓膨胀，当效果数翻倍到 100+ 时终究会撞上天花板；而按需注入的架构模式具有通用性——未来新增"礼装 hints"、"关卡 hints"时，各 hints 块独立按需注入，互不干扰。

**改良实现：启发式预判 + 按需注入（零额外 LLM 调用）**：

```
用户查询 → 关键词启发式预判（零 LLM 成本）→ 按需注入对应 hints → 一次路由
```

```python
def _should_inject_effect_hints(user_message: str) -> bool:
    """启发式判断是否需要注入效果语义表。"""
    effect_keywords = ["效果", "技能", "能力", "增伤", "无敌", "闪避",
                       "充能", "加攻", "魔放", "特攻", "宝具效果", ...]
    return any(kw in user_message for kw in effect_keywords)
```

- **命中**（~40% 查询）：注入完整效果 hints
- **未命中**（~60% 查询）：不注入，路由 Prompt 节省 ~35%
- **误判兜底**：即使启发式没命中，LLM 仍能看到 Skill description，基本效果查询仍可路由

**关键词表存放**：`server/config/routing_hints_triggers.json`，与代码解耦。未来每新增一个领域的 hints 块，只需新增一组对应的触发关键词。

| 领域 hints | 触发关键词示例 |
|:---|:---|
| 效果 hints（当前） | "效果", "技能", "无敌", "加攻", "魔放"... |
| 礼装 hints（未来） | "礼装", "概念礼装", "黑圣杯", "限凸"... |
| 关卡 hints（未来） | "关卡", "副本", "自由本", "种火"... |

---

### 原方案 D 细节（供参考，不采纳）

以下为原方案 D 的详细设计，作为备选记录。

### 具体优化细节

**效果 hints 压缩（方案 B）**：

当前格式：
```
- `upAtk`: 攻击力提升 / 加攻 — 提升攻击力，增加所有攻击的基础伤害
```

压缩为：
```
- `upAtk`: 攻击力提升 / 加攻
```

去掉 description 部分（"— 提升攻击力，增加..."），因为 LLM 通过中文别名已经能理解效果语义，description 更多是给人看的文档。

**few-shot 精简（方案 C）**：

保留 5 个核心示例（覆盖主要查询模式），移除 7 个可由规则推导的示例：

| 保留 | 理由 |
|:---|:---|
| NP 自充 + 职阶组合 | 最常见的多 Skill AND 组合 |
| 单从者查询（lookup） | 区别于筛选类 |
| 效果查询（search_by_effect） | 默认效果路由 |
| 从者对比（compare） | 独特模式 |
| 虚拟复合效果（damageShield） | 需要示例引导 |

移除的 7 个示例都可以由路由规则 + Skill description 推导：
- 精确技能效果查询（规则 8 已覆盖）
- 精确宝具效果查询（规则 8 已覆盖）
- 蓝魔放五星从者（效果查询变种）
- 解除负面状态（效果查询变种）
- 量化条件示例 x2（规则 10 已覆盖）
- 全队加攻示例（量化变种）

---

## 优化方向二：Eval-First 路由测试（P1）

### Perplexity 的核心洞察

> "Step 0：先写 Eval。至少要保证你测试的是 Skill 在该加载的时候确实加载了。反面例子的威力极大，有时候比正面例子还重要。"

### 我们的问题

当前测试体系存在一个巨大盲区：**路由准确性完全没有自动化 eval**。

| 测试类型 | 当前覆盖 | 缺失 |
|:---|:---|:---|
| Skill 执行逻辑（filter） | `test_skill_api.py` ✅ | — |
| Skill 框架（注册/分组） | `test_skill_framework.py` ✅ | — |
| Schema 契约 | `test_schemas.py` ✅ | — |
| **路由准确性** | **无** ❌ | 用户查询 → 期望 Skill 选择 |
| **路由反面例子** | **无** ❌ | 不该路由到某 Skill 的查询 |
| **邻近域混淆** | **无** ❌ | 如"配卡" vs "色卡增伤" |

### 方案

新增 `tests/test_routing_eval.py`，建立路由 eval 套件：

```python
# 正面例子：期望路由到 search_by_effect
POSITIVE_CASES = [
    ("有增伤技能的从者", ["search_by_effect"], {"effect": "damageBoost"}),
    ("能挡伤害的从者", ["search_by_effect"], {"effect": "damageShield"}),
    ...
]

# 反面例子：不该路由到某 Skill
NEGATIVE_CASES = [
    ("梅林厉害吗", "search_by_effect"),  # 应该是 lookup，不是效果搜索
    ...
]

# 邻近域混淆：容易混淆的查询对
CONFUSION_PAIRS = [
    ("蓝卡多的从者", "search_by_cards"),      # 配卡查询
    ("有蓝魔放的从者", "search_by_effect"),    # 效果查询，不是配卡
    ...
]
```

**实现方式**：
- 默认标记为 `@pytest.mark.skipif(not os.getenv("RUN_LIVE_LLM_TESTS"))`，避免日常测试消耗 LLM quota
- 每次新增 Skill 或修改路由规则时，手动运行一次验证
- 可选：记录历史 routing_output 日志作为"录制回放"式 eval，零 LLM 消耗

### 讨论结论

**采用混合粒度 eval 策略**：粗粒度 Skill 选择 + 反面断言，不测精确参数值。

| 维度 | 粗粒度（只测 Skill 选择） | 细粒度（同时测参数） |
|:---|:---|:---|
| **维护成本** | **低** — 只需维护 skill_name 列表 | **高** — 参数值容易因 Prompt 调整而变化 |
| **脆性** | **低** — Skill 选择是稳定的 | **高** — 参数值受模型版本影响大，容易假阴性 |
| **覆盖的 Bug 类型** | 路由错误（选错/漏选 Skill） | 路由错误 + 参数错误 |

决策理由：
1. **路由选择是最高杠杆的测试**：选错 Skill = 结果完全错误，这是最严重的 Bug
2. **参数准确性大部分由 Pydantic schema 兜底**：`params_schema` 校验会拦住格式错误
3. **全量参数断言太脆**：模型换版本、Prompt 微调都可能改变参数格式，导致 eval 频繁误报

混合策略示例：

```python
# 粗粒度：只断言 Skill 选择
("有增伤技能的从者", {"expected_skills": ["search_by_skill_effect"]}),

# 反面断言：不该选什么
("蓝卡多的从者", {
    "expected_skills": ["search_by_cards"],
    "forbidden_skills": ["search_by_effect"],
}),

# 邻近域混淆：只断言不该选什么
("配卡是三蓝的从者", {
    "forbidden_skills": ["search_by_effect"],
}),
```

核心理念：测「选对了什么」和「不该选什么」（正面+反面），不测「参数精确等于什么」。

---

## 优化方向三：description 重写为触发器（P2）

### Perplexity 的核心洞察

> "差的描述写'这个 Skill 是干嘛的'。好的描述写'什么时候 Agent 应该加载它'。用 'Load when...' 开头。"

### 我们的问题

当前所有 Skill 的 description 都是功能说明式的：

| Skill | 当前 description | 问题 |
|:---|:---|:---|
| `search_by_class` | "按职阶筛选从者（如 Saber、Caster）" | 是文档，不是触发器 |
| `search_by_effect` | "按效果筛选从者，默认同时搜技能效果和宝具效果" | 是文档 |
| `search_by_cards` | "按配卡组合、宝具颜色、宝具目标筛选从者" | 是文档 |
| `search_by_np_charge` | "按 NP 充能量筛选从者（如自充 ≥ 50%）" | 部分触发器意味 |

### 方案

将 description 从"功能说明"改为"触发条件"，更贴近用户的真实查询意图：

| Skill | 建议新 description |
|:---|:---|
| `search_by_class` | "当用户提到职阶名称（如 Saber、Caster、骑阶、术阶、狂阶等）时使用" |
| `search_by_effect` | "当用户查询某种效果或能力（如无敌、加攻、充能、增伤等），且未指定来源（技能/宝具）时使用" |
| `search_by_cards` | "当用户提到配卡构成（如几蓝几红）、宝具颜色（红宝具）、宝具目标（全体/单体）时使用" |
| `search_by_np_charge` | "当用户提到自充、充能、NP 充电、XX% 充能时使用" |
| `search_by_rarity` | "当用户提到星级/稀有度（如五星、4星、金卡等）时使用" |
| `lookup_servant` | "当用户询问特定一个从者的信息（如'查一下梅林'、'孔明怎么样'）时使用" |
| `compare_servants` | "当用户想对比两个或多个从者（如'对比梅林和孔明'、'村正和武尊谁强'）时使用" |

**注意事项**：
- Perplexity 警告"description 里的小幅措辞调整，对路由的影响往往是巨大的"
- 必须配合 Eval 套件（优化方向二）验证改动不会破坏现有路由

### 讨论结论

**逐个 Skill 切换，每改一个配合 eval 验证。**

决策理由：如果一次性改 15 个 Skill 的 description，某个改坏了但 eval 通过了（假阴性），根本不知道是哪个出了问题。逐个改的好处是每次变更的 diff 范围小，出问题能立刻定位到具体 Skill。

建议执行顺序（从使用频率最高 + 邻近域混淆风险最大的 Skill 开始）：

| 批次 | Skill | 原因 |
|:---|:---|:---|
| 1 | `search_by_effect` | 最常用 + 与 cards/skill_effect/np_effect 混淆风险高 |
| 2 | `search_by_cards` | 与 effect 的邻近域混淆（色卡性能 vs 配卡） |
| 3 | `lookup_servant` / `compare_servants` | 最容易与筛选类 Skill 混淆 |
| 4 | 其余 Query Skills | 风险较低，可以 2-3 个一批 |

---

## 优化方向四：Gotcha 飞轮机制（P2）

### Perplexity 的核心洞察

> "Skill 是只增不删（append-mostly）的。时间一长，最有价值积累的就是 gotcha 这一节。Agent 翻车一次就加一条 gotcha。"

### 我们的问题

当前的"坑"散落在路由 Prompt 的全局规则中：

- **规则 5**：色卡性能提升 vs 配卡查询的区分 → 应属于 `search_by_cards` 和 `search_by_effect` 的 gotcha
- **规则 9**：禁止同 Skill 多次调用表达 OR → 应属于效果类 Skill 的 gotcha
- **规则 8**：效果类查询的 Skill 选择 → 应属于 `search_by_effect` / `search_by_skill_effect` / `search_by_np_effect` 三者的 gotcha

这种做法导致两个问题：
1. 全局规则越来越多，路由 Prompt 持续膨胀
2. 新增 Skill 时无法判断哪些规则跟自己相关

### 方案

在 `BaseSkill` 中增加 `gotchas` 属性，将场景特定的规则下沉到 Skill 级别：

```python
class BaseSkill:
    name: str = ""
    description: str = ""
    gotchas: list[str] = []  # 新增
```

路由 Prompt 构建时，将 gotcha 按 Skill 分组注入，而不是作为全局规则：

```
## 可用 Skills
- `search_by_effect`: 当用户查询某种效果或能力时使用
  ⚠ 涉及色卡性能提升时用此 Skill，不要用 search_by_cards
  ⚠ 多效果 OR 用 effects+effectsOp:"or"，禁止多次调用同 Skill
```

**收益**：
- 全局规则从 10 条减少到 ~4 条（仅保留真正全局的规则）
- 每个 Skill 的 gotcha 紧跟 description，LLM 更容易关联
- 新增 Skill 时，gotcha 自然归属到具体 Skill，不污染全局

### 飞轮机制

建立 gotcha 积累流程（纳入 AGENTS.md）：

```
线上发现路由错误 → 分析根因 → 追加到对应 Skill 的 gotchas 列表 
→ 添加 eval 反面例子 → 验证修复
```

### 讨论结论

**Gotcha 紧跟 description 注入。**

| 方式 | 优点 | 缺点 |
|:---|:---|:---|
| 紧跟 description | LLM 看到 Skill 时同时看到陷阱，关联性强；Skill 自包含，开源贡献者只需在自己的 Skill 文件里写 gotcha | 如果 gotcha 多了会让 Skill 列表较长 |
| 独立 section | 结构清晰，不干扰 Skill 列表 | LLM 可能在选 Skill 时忽略末尾注意事项；新增 Skill 时需去全局 section 加规则 |

决策理由：紧跟 description 模式下，每个 Skill 的 gotcha 是自包含的，贡献者新增 Skill 时只需在自己的 Skill 文件里写 gotcha，不需要去全局 section 里加规则。这对开源可扩展性更友好。

---

## 优化方向五：Generation Prompt 分层（P3）

### 问题

当前 `get_generation_prompt()` 包含 9 条规则 + 检查清单，**每次生成都全量注入**（~2,300 token）。但部分规则只对特定 Response Skill 有意义：

| 规则 | 适用场景 | 不适用场景 |
|:---|:---|:---|
| 规则 2（结合全局统计） | `respond_servant_list` | `respond_servant_detail`（单从者无统计） |
| 规则 4 中"禁止以偏概全" | `respond_servant_list` | `respond_servant_compare`（不存在代表性问题） |
| 规则 7（能力边界） | 所有 | — |
| 规则 8（零技术术语） | 所有 | — |

### 方案

将 generation prompt 拆分为：

- **核心规则**（~5 条，始终注入）：规则 1/3/7/8/9 + 检查清单
- **列表场景规则**（按需注入）：规则 2（全局统计）、规则 4 后半段（禁止以偏概全）
- **特化补充**（已有机制）：各 Response Skill 的 `_DETAIL_SUPPLEMENT` 等

```python
class ResponseSkill(BaseSkill):
    @abstractmethod
    def build_prompt(self, user_message: str, context_json: str) -> str:
        ...

    @property
    def extra_rules(self) -> list[str]:
        """此 Response Skill 需要的额外规则索引。"""
        return []
```

**预计收益**：对 `respond_servant_detail` 和 `respond_servant_compare` 场景节省 ~300-500 token。

### 讨论结论

**纳入执行计划，与其他方向同步推进。**

虽然当前 Response Skill 只有 4 个、收益有限，但用户决策：既然未来一定要拓展，不如现在就打好分层基础，避免后续 Response Skill 增加到 6+ 时再做拆分带来的迁移成本。

实现方案确认：
- 核心规则（~5 条，始终注入）：规则 1/3/7/8/9 + 检查清单
- 场景规则：各 Response Skill 通过 `extra_rules` 属性声明自己需要的额外规则
- 特化补充：保留已有的 `_DETAIL_SUPPLEMENT` 等机制

---

## 整体优先级与执行计划

### 依赖关系

```mermaid
graph TD
    A[方向二: Eval-First 路由测试] --> B[方向三: description 逐个重写]
    A --> C[方向四: Gotcha 飞轮]
    A --> D[方向一: 路由 Prompt 按需注入]
    B --> D
    C --> D
    E[方向五: Generation Prompt 分层]
    
    style A fill:#ff6b6b,color:#fff
    style D fill:#ff6b6b,color:#fff
    style B fill:#ffa500,color:#fff
    style C fill:#ffa500,color:#fff
    style E fill:#4ecdc4,color:#fff
```

### 决策总表

| 方向 | 选定方案 | 预计 Token 节省 | 前置依赖 | 执行策略 |
|:---|:---|:---|:---|:---|
| **方向二** | 混合粒度 eval（Skill 选择 + 反面断言） | 0（基础设施） | 无 | **最先执行** |
| **方向一** | 方案 A（启发式按需注入），关键词表存 `config/` | ~35%（平均，~1,400 token） | 方向二 | 方向二之后 |
| **方向三** | 逐个 Skill 切换 description 为触发器 | 间接（提升路由精度） | 方向二 | 逐个改 + eval 验证 |
| **方向四** | 紧跟 description 注入 gotcha | ~200-400 token（全局规则下沉） | 方向二 | Skill 自包含 |
| **方向五** | Generation Prompt 分层（核心规则 + 场景规则） | ~300-500 token/次 | 无 | 当前就执行 |

---

## 不采纳的 Perplexity 理念

| 理念 | 不采纳原因 |
|:---|:---|
| **Skill 是一个目录**（SKILL.md + scripts/ + references/ + assets/） | Perplexity 的 Skill 是文档型（LLM 阅读），我们的 Skill 是代码型（Python 类执行）。当前 `query/` + `response/` 分目录已足够 |
| **config.json 用户配置** | 我们是单用户助手，不需要 per-user Skill 配置 |
| **depends: Skill 依赖链** | 当前 Skill 粒度较小且相互独立，不需要层级依赖 |
| **让 LLM 自己写 Skill** | 原文引用研究结论"模型自己生成的 Skill 平均没有任何效果"，确认不走这条路 |

---

## 待决策问题（讨论状态）

1. ~~**方向一的方案选择**~~：✅ 已决策 → 选方案 A（启发式按需注入），关键词表存 `config/routing_hints_triggers.json`
2. ~~**方向三的 description 重写**~~：✅ 已决策 → 逐个 Skill 切换，每改一个配合 eval 验证
3. ~~**方向四的 gotcha 注入方式**~~：✅ 已决策 → 紧跟 description 注入，Skill 自包含
4. ~~**方向二的 eval 粒度**~~：✅ 已决策 → 混合粒度（粗粒度 Skill 选择 + 反面断言，不测精确参数值）
5. ~~**方向五的执行时机**~~：✅ 已决策 → 当前就执行，不等 Response Skill 增长

**所有方向均已达成结论，可进入实施阶段。**
