# ADR-013: Skill 架构与 AI 技术方案讨论

**日期**: 2026-05-06  
**状态**: 已演进至 ADR-018 (Superseded by ADR-018)  
**参与者**: 用户、AI Agent  
**相关 Issue**: Phase 5 架构治理、Phase 6 扩展规划  
**后续文档**: [ADR-018 Skill-Based Architecture 架构决策](../adr/ADR-018-skill-based-architecture.md)  

---

## 一、问题背景

### 1.1 用户提出的核心痛点

在实施 Phase 5.5（名称匹配增强）后，用户指出：

> "如果每次遇到一个新的场景对话，我都需要进来修改提示词、脚本，这种单独支持的成本太高了。"

**具体表现**：
1. 新增"多从者对比"功能时，修改了 4 个文件：
   - `server/prompts.py`（添加对比规则）
   - `server/schemas.py`（添加 `names` 字段）
   - `server/query_executor.py`（添加多从者查询逻辑）
   - `server/main.py`（如需调整路由）

2. 每次新增场景的维护成本：3-6 小时/个

3. 代码耦合度高：
   - Prompts 硬编码 219 行
   - Query Executor 单体 343 行，圈复杂度 25+
   - Schema 持续膨胀

### 1.2 对话记忆缺失

日志分析显示（`query_trace.jsonl` 第 25-28 行）：

```
Round 1: "对比一下千子村正和武尊两个从者"
  → LLM 只识别出 "千子村正"，忽略了 "武尊"

Round 2: "大和武尊"
  → 用户单独查询，成功找到

Round 3: "对比一下千子村正和大和武尊两个从者"
  → LLM 将 "千子村正 大和武尊" 作为整体名称搜索
  → 返回 0 个结果（失败）
```

**根因**：每次请求独立，无上下文记忆。

---

## 二、架构方案讨论

### 2.1 核心问题

用户提问：
> "是否有必要抽象一些必要的 skill 和 mcp，再基于 skill 和 mcp 组合加工成模块化的产品？"

### 2.2 方案关系梳理

**重要说明**：以下方案**不是互斥的**，而是**可以组合使用的不同层次的技术**。

```
┌─────────────────────────────────────────────────────────┐
│                   应用架构层                              │
│                                                         │
│  Skill 系统（模块化）                                    │
│  ├─ 解决：代码组织、扩展性、维护成本                      │
│  └─ 替代方案：完整 MCP 协议（当前不需要）                 │
└──────────────────┬──────────────────────────────────────┘
                   ↓
┌─────────────────────────────────────────────────────────┐
│                   AI 能力层                               │
│                                                         │
│  1. RAG 增强检索（实体识别）                              │
│     ├─ 解决：名称匹配、别名自动学习                       │
│     └─ 可与 Skill 系统组合使用                            │
│                                                         │
│  2. Agent 工作流（多步推理）                              │
│     ├─ 解决：复杂任务（如组队推荐）                       │
│     └─ 本质上是一个复杂的 Skill                           │
│                                                         │
│  3. 对话状态机（多轮对话）                                │
│     ├─ 解决：参数缺失时的追问                            │
│     └─ 可与所有 Skill 配合使用                           │
│                                                         │
│  4. 反馈学习机制（持续优化）                              │
│     ├─ 解决：长期准确率提升                              │
│     └─ 可作用于所有 Skill 和 Agent                       │
└─────────────────────────────────────────────────────────┘
```

---

### 2.3 核心架构：轻量级 Skill 系统（✅ 必选）

**定位**：项目的基础架构，解决代码组织和扩展性问题。

**核心思想**：
- 不引入完整的 MCP 协议
- 实现简化的 Skill 注册 + 调用机制
- 每个 Skill 独立文件、独立测试、独立部署

**架构设计**：

```
server/
├── skills/                    # 新增：技能模块目录
│   ├── __init__.py
│   ├── base.py               # Skill 基类
│   ├── registry.py           # 技能注册表
│   ├── servant_query.py      # 单从者查询
│   ├── servant_compare.py    # 多从者对比
│   └── material_calc.py      # 素材计算（未来）
├── memory.py                 # 新增：对话记忆管理
├── schemas.py                # 简化：只保留通用结构
├── prompts.py                # 简化：动态生成 Prompt
├── query_executor.py         # 保持不变（被 Skills 调用）
└── main.py                   # 重构：Skill 路由层
```

**改造成本**：
- 工作量：~13 小时（约 2 个工作日）
- 风险：低（渐进式迁移，不破坏现有功能）

**收益**：
- 新增功能成本降低 50-70%（从 3-6 小时降至 1-3 小时）
- 代码隔离：每个 Skill 独立维护
- 易于测试：每个 Skill 可单独测试
- 未来兼容：预留 MCP 协议对接接口

---

#### 替代方案：完整 MCP 协议（❌ 不推荐当前阶段）

**MCP（Model Context Protocol）**：Anthropic 提出的标准协议，让 LLM 能够动态发现和调用外部工具。

**优势**：
- ✅ 标准化协议（Claude、GPT 都支持）
- ✅ 动态工具发现（LLM 自动学习新工具）
- ✅ 生态丰富（已有大量 MCP Server）

**劣势**：
- ❌ 实现复杂度高（需要实现 MCP Server 协议）
- ❌ 依赖外部库（`mcp` Python 包）
- ❌ 对于当前规模（4 个技能）过度设计

**结论**：先用轻量级 Skill 系统，未来如果需要对接多个外部工具时再迁移到 MCP。

---

### 2.4 AI 能力增强方案（✅ 可选组合）

以下方案可以**与 Skill 系统组合使用**，提供不同的 AI 能力增强：

#### 能力 1：RAG 增强检索（⭐⭐⭐⭐⭐ 强烈推荐）

**解决的问题**：
- 昵称映射需要手动维护 `nicknames.json`
- "武尊" → "大和武尊" 需要硬编码子串匹配

**技术方案**：
```python
# 使用轻量级向量检索做实体识别
from sentence_transformers import SentenceTransformer
import faiss

class EntityRecognizer:
    """基于向量的实体识别器"""
    
    def __init__(self):
        self.model = SentenceTransformer('all-MiniLM-L6-v2')  # 22MB
        self.index = None
        self.entities = []  # 所有从者名称、别名、昵称
    
    def build_index(self, servants: list[dict], nicknames: dict):
        """构建实体索引（启动时调用一次）"""
        # 收集所有名称变体
        for svt in servants:
            self.entities.extend([
                svt['name'],
                svt.get('aliasCN', ''),
                svt.get('originalName', '')
            ])
        self.entities.extend(nicknames.keys())
        
        # 生成 embedding + 构建 FAISS 索引
        # ...
    
    def recognize(self, query: str, top_k: int = 3) -> list[tuple[str, float]]:
        """识别查询中的实体"""
        # 向量相似度搜索
        # ...
```

**成本**：
- 实现：8 小时
- 运行时：CPU 可运行，~100MB 内存增加
- 依赖：`faiss-cpu`, `sentence-transformers`

**收益**：
- 自动学习别名关系，无需手动维护
- 支持错别字、同音字
- 昵称维护成本降低 80%

**触发条件**（何时需要引入）：
- ✅ 用户频繁提问："找一个**类似**诸葛亮的从者"
- ✅ 用户提问："有**类似**呆毛王技能的从者"
- ❌ 仅仅是名称匹配 → 当前子串匹配已解决

---

#### 能力 2：Agent 工作流（⭐⭐⭐⭐ 强烈推荐）

**定位**：解决复杂多步任务，本质上是**一个复杂的 Skill**。

**场景**：用户提问"帮我组建一个适合新手的蓝卡队"

**当前架构**：无法处理（需要多步推理）

**Agent 方案**：
```python
class TeamBuilderAgent:
    """队伍组建 Agent（多步推理）"""
    
    async def execute(self, user_request: str) -> dict:
        # 步骤 1: 解析需求
        requirements = await self.parse_requirements(user_request)
        
        # 步骤 2: 查询候选从者
        candidates = execute_query({...})
        
        # 步骤 3: 评估队伍适配度
        scored_teams = await self.evaluate_teams(candidates)
        
        # 步骤 4: 生成建议
        return await self.generate_recommendation(scored_teams)
```

**成本**：20 小时  
**收益**：支持复杂多步任务

---

#### 能力 3：对话状态机（⭐⭐⭐⭐⭐ 强烈推荐）

**定位**：解决多轮对话无状态问题，可与**所有 Skill 配合使用**。

**解决的问题**：多轮对话无状态

**技术方案**：
```python
class DialogueState:
    """对话状态管理"""
    
    def __init__(self):
        self.current_intent: str | None = None
        self.collected_slots: dict = {}
        self.missing_slots: list[str] = []
    
    def update(self, parsed_intent: dict):
        """更新对话状态"""
        # 检查是否缺少必要参数
        # 如果需要，生成追问 Prompt
    
    def needs_clarification(self) -> bool:
        """是否需要追问用户"""
        return len(self.missing_slots) > 0
```

**使用场景**：
```
用户: "对比一下从者"
系统: "请问您想要对比哪些从者？"  ← 状态机发现缺少 names 参数
用户: "千子村正和大和武尊"
系统: [执行对比]
```

**成本**：6 小时  
**收益**：多轮对话成功率显著提升

---

#### 能力 4：反馈学习机制（⭐⭐⭐ 可选）

**定位**：长期优化，可作用于**所有 Skill 和 Agent**。

**场景**：用户对搜索结果不满意

**技术方案**：
```python
class FeedbackCollector:
    """用户反馈收集与学习"""
    
    async def collect_feedback(self, trace_id: str, feedback: str):
        """收集用户反馈"""
        if feedback in ["不对", "错了", "不是这个"]:
            # 触发 LLM 重新理解
            await self.retry_with_feedback(trace_id, feedback)
```

**成本**：10 小时  
**收益**：长期准确率提升

---

### 2.5 方案组合建议

根据项目的实际需求，推荐以下组合方案：

#### 组合 A：基础版（立即实施）

**包含**：
- ✅ Skill 系统（必选）
- ✅ 对话记忆（必选）
- ✅ 对话状态机（强烈推荐）

**工作量**：23 小时（约 3 工作日）  
**解决**：
- 代码组织与扩展性问题
- 多轮对话无记忆问题
- 参数缺失时的追问机制

**适用场景**：满足 90% 的日常查询需求

---

#### 组合 B：增强版（短期优化）

**包含**：
- ✅ 组合 A 的全部内容
- ✅ RAG 增强检索（强烈推荐）

**工作量**：31 小时（约 4 工作日）  
**额外解决**：
- 昵称手动维护问题
- 错别字、同音字容错
- 实体识别准确率提升

**适用场景**：用户量增长后，降低维护成本

---

#### 组合 C：完整版（中期规划）

**包含**：
- ✅ 组合 B 的全部内容
- ✅ Agent 工作流（强烈推荐）
- ✅ 反馈学习机制（可选）

**工作量**：61 小时（约 8 工作日）  
**额外解决**：
- 复杂多步任务（组队推荐、素材计算）
- 长期准确率持续优化

**适用场景**：功能覆盖度对标 Chaldea

---

### 2.6 方案之间的关系总结

```
Skill 系统（基础架构）
    ├─ 可与 RAG 增强检索组合 → 提升实体识别准确率
    ├─ 可与 Agent 工作流组合 → Agent 本质是复杂 Skill
    ├─ 可与对话状态机组合 → 所有 Skill 都受益
    └─ 可与反馈学习组合 → 所有 Skill 持续优化

RAG 增强检索
    └─ 服务于 Skill 系统（特别是 servant_query）

Agent 工作流
    ├─ 本质是一个 Skill
    └─ 内部可调用其他 Skills

对话状态机
    └─ 作用于所有 Skills 的参数收集阶段

反馈学习
    └─ 作用于所有 Skills 和 Agents 的结果优化
```

---

## 三、实施计划建议

### Phase 5.6 — Skill 架构 + 对话记忆（建议下一个 Phase）

| 步骤 | 内容 | 工作量 |
|:----|:----|:------:|
| 1 | 创建 Skill 基础设施（base.py, registry.py） | 4h |
| 2 | 迁移现有查询逻辑为 Skills | 3h |
| 3 | 实现对话记忆管理器 | 4h |
| 4 | 重构 main.py 路由层 | 3h |
| 5 | 测试验证 | 3h |
| **总计** | | **17h (~2.5 工作日)** |

**新增文件**：
```
server/
├── skills/
│   ├── __init__.py
│   ├── base.py               # Skill 基类 + SkillResult
│   ├── registry.py           # SkillRegistry 单例
│   ├── servant_query.py      # ServantQuerySkill
│   └── servant_compare.py    # ServantCompareSkill
└── memory.py                 # ConversationMemory
```

**修改文件**：
```
server/main.py                # 重构为 Skill 路由层
server/prompts.py             # 简化为动态生成
server/schemas.py             # 简化为通用结构
```

---

### Phase 5.7 — RAG 实体识别（可选）

| 步骤 | 内容 | 工作量 |
|:----|:----|:------:|
| 1 | 集成 sentence-transformers + FAISS | 2h |
| 2 | 构建实体索引 | 2h |
| 3 | 替换当前子串匹配逻辑 | 2h |
| 4 | 测试调优 | 2h |
| **总计** | | **8h (~1 工作日)** |

---

### Phase 6 — Agent 工作流 + 对话状态机

| 步骤 | 内容 | 工作量 |
|:----|:----|:------:|
| 1 | 实现对话状态机 | 6h |
| 2 | 实现 TeamBuilderAgent | 8h |
| 3 | 实现 MaterialCalcAgent | 6h |
| **总计** | | **20h (~3 工作日)** |

---

## 四、关键决策记录

### 决策 1：选择轻量级 Skill 系统而非完整 MCP

**原因**：
1. 当前只有 2-4 个技能场景，MCP 过度设计
2. 轻量级系统实现成本低（13 小时 vs 30+ 小时）
3. 预留了未来迁移到 MCP 的接口
4. 不引入外部依赖（`mcp` 包）

**迁移路径**：
```
轻量级 Skill 系统 (Phase 5.6)
    ↓ (当技能数量 > 10 或需要对接外部工具时)
完整 MCP 协议 (Phase 8+)
```

---

### 决策 2：对话记忆采用简单历史记录而非向量检索

**原因**：
1. 当前只需支持最近 10 轮对话
2. 简单列表存储足够（内存占用 < 1MB）
3. 无需引入额外的向量数据库
4. 实现简单，易于调试

**未来扩展**：
- 如果需要长期记忆（跨会话）→ 引入向量数据库
- 如果需要语义检索历史 → 引入 embedding

---

### 决策 3：RAG 实体识别作为可选方案

**原因**：
1. 当前子串模糊匹配已解决 90% 的名称匹配问题
2. 向量检索引入额外依赖和运行时成本
3. 仅在"语义相似搜索"场景下才有明显收益

**触发条件**：
- 用户频繁提问"找类似的从者"
- 昵称维护成本超过 500+ 别名
- 需要支持错别字、同音字容错

---

## 五、未来研究方向

### 5.1 向量化技能组合

**问题**：当前 Skills 是离散调用的，无法自动组合

**未来方案**：
```python
# 用户输入："帮我找 30 自充的蓝卡从者，然后对比他们的宝具"
# 自动分解为：
skills = [
    ("servant_query", {"npCharge": {"op": "eq", "value": 30}, "npCard": "arts"}),
    ("servant_compare", {"names": [...]})  # 使用上一步的结果
]
```

**技术**：LLM 自动规划 + 工具调用链

---

### 5.2 技能推荐系统

**问题**：用户不知道有哪些可用技能

**未来方案**：
```python
# 基于用户输入历史，推荐可能感兴趣的 Skills
def recommend_skills(user_history: list[str]) -> list[str]:
    # 使用协同过滤或内容推荐
    # ...
```

---

### 5.3 技能市场（Skill Marketplace）

**问题**：社区贡献的 Skills 如何集成

**未来方案**：
```
server/
├── skills/
│   ├── builtin/              # 内置 Skills
│   │   ├── servant_query.py
│   │   └── ...
│   └── community/            # 社区 Skills（可选安装）
│       ├── team_builder.py
│       └── damage_calc.py
```

---

## 六、参考资料

- **MCP 协议**: https://modelcontextprotocol.io/
- **FAISS**: https://github.com/facebookresearch/faiss
- **Sentence Transformers**: https://www.sbert.net/
- **OpenAI Responses API**: https://developers.openai.com/api/reference/responses/overview

---

## 七、后续行动

- [ ] 用户确认是否立即实施 Phase 5.6（Skill 架构）
- [ ] 如确认，创建详细实施计划
- [ ] 开始编码：创建 `server/skills/` 目录结构
- [ ] 迁移现有查询逻辑为 Skills
- [ ] 实现对话记忆管理器
- [ ] 运行测试验证

---

**文档维护者**: AI Agent  
**最后更新**: 2026-05-06  
**下次 Review**: Phase 5.6 实施前
