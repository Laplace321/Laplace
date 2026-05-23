# A→B→C 三级知识问答链路架构讨论

> 状态: **已达成结论** | 日期: 2026-05-23 | 参与者: 羽殊 + AI

## 背景与动机

trace 8589d27a 暴露了 Agent 回答质量问题：LLM 编造了错误的灵衣信息，缺乏知识库支撑和后置校验。当前系统仅有链路 A（Skill 精确查询），对于非结构化知识问答（活动历史、关卡攻略、配队推荐等）完全依赖 LLM 自身知识，幻觉风险高。

需要设计一套多层知识问答体系，覆盖从精确数据查询到主观攻略建议的完整光谱。

## 已达成结论

### 1. 三级链路串行架构：A → B → C

```
用户提问
   │
   ▼
┌─────────────────────────────────────────────┐
│  链路 A：Skill 精确查询（现有 OneShot 路由）   │
│  数据源：servants_db.json + Skill 模块        │
│  覆盖：从者属性/技能效果/礼装/特性/职阶克制     │
│  特点：结构化精确匹配，零幻觉风险              │
└──────────────────┬──────────────────────────┘
                   │ A 无结果 or 非 servant domain
                   ▼
┌─────────────────────────────────────────────┐
│  链路 B：Atlas 知识问答                       │
│  数据源：Atlas CN API 索引                    │
│  覆盖：活动/卡池/主线关卡/素材掉落/版本历史     │
│  检索：结构化字段倒排索引 + 关联反查            │
│  校验：轻量级索引验证（NER+正则提取 → 索引查表）│
└──────────────────┬──────────────────────────┘
                   │ B 无结果 or 非 atlas domain
                   ▼
┌─────────────────────────────────────────────┐
│  链路 C：攻略知识问答                          │
│  数据源：server/data/guides/*.md              │
│  覆盖：关卡攻略/配队推荐/玩法分析/主观评价      │
│  检索：关键词(BM25)优先 + 向量语义兜底          │
│  校验：来源标注 + 免责声明（不做事实校验）       │
└─────────────────────────────────────────────┘
```

### 2. 各链路核心区别

| 维度 | 链路 A | 链路 B | 链路 C |
|:-----|:------|:------|:------|
| 数据形态 | 结构化 JSON | 半结构化 Atlas CN JSON | 非结构化 Markdown |
| 检索方式 | Skill 参数化精确查询 | 字段倒排索引 + 关联反查 | BM25 + 向量 ANN |
| 回复生成 | ResponseSkill 模板化 | LLM 生成 + 索引校验 | LLM 生成 + 来源标注 |
| 校验机制 | 无需（数据即事实） | 轻量级：NER+正则 → 索引查表 | 仅来源标注 |
| Token 成本 | 低 | 中（索引摘要注入） | 中高（文档块注入） |
| 延迟 | 最低 | 中（索引查找 <1ms） | 中（BM25 5ms + 向量 50ms） |
| 幻觉风险 | 几乎为零 | 低（有索引锚点） | 中（攻略可能过时） |

### 3. 检索方案选型

#### 链路 B：结构化字段倒排索引

- **数据源**：Atlas CN Export API（原生中文，无需翻译）
  - `export/CN/nice_event.json` (1889 items)
  - `export/CN/nice_war.json` (212 items)
  - `export/CN/nice_gacha.json` (2769 items)
  - `export/CN/nice_item.json` (待确认)
- **索引结构**：
  - `name_index`: 名称 → ID 列表（模糊匹配）
  - `tag_index`: 类型标签 → ID 列表
  - `time_index`: 年月 → ID 列表
  - `servant_event_index`: servant_id → event_id 列表（关联反查）
  - `servant_gacha_index`: servant_id → gacha_id 列表
- **查询方式**：路由 LLM 输出结构化 `AtlasQueryParams`，字段级匹配取交集
- **不需要 Embedding**：结构化数据用字段匹配即可，语义检索无优势

#### 链路 C：关键词优先 + 向量兜底（混合检索）

- **数据源**：`server/data/guides/*.md`（YAML frontmatter + Markdown 正文）
- **主检索**：BM25 关键词匹配（`rank_bm25` 库，纯内存，<5ms）
- **兜底检索**：当 BM25 置信度低时，触发向量语义检索（Embedding + FAISS/numpy）
- **一期就搭好完整架构**：向量层预留接口，即使初期不用也要把管道建好
- **文档切分**：按 `##` 标题切分为文档块，每块 ~300-500 tokens

### 4. 后置校验分层策略

#### 链路 B：轻量级索引验证（非 LLM）

```
LLM 回复 → NER+正则提取实体（从者名/活动名/时间/数值）
         → 查 Atlas 索引验证
         → 全部通过 → 放行
         → 有不一致 → 标记修正提示注入回复
```

- 事实提取：名称表查找 + 正则，零 LLM 成本
- 事实验证：索引直接查，确定性结果，<10ms
- 比"二次 LLM 调用"快 10x、便宜 100x

#### 链路 C：仅来源标注

- 攻略内容本身可能有误/过时/有争议，不适合做事实校验锚点
- 回复末尾注明「以上内容基于攻略《{source_title}》」
- 主观评价类问题明确告知「这是攻略作者的观点，仅供参考」

### 5. Preset 直连 B/C 链路

Preset 新增 `target_pipeline` 字段，跳过不必要的 A 链路：

```python
@dataclass
class Preset:
    name: str
    display_name: str
    query_skills: list[str]
    response_skill: str = "respond_servant_list"
    param_template: dict[str, dict] = field(default_factory=dict)
    target_pipeline: str = "A"  # 新增：A/B/C
    guide_tags: list[str] = field(default_factory=list)  # C 链路 tag 预过滤
```

- `target_pipeline="A"`：现有逻辑不变（默认值，向后兼容）
- `target_pipeline="B"`：跳过 SkillExecutor，直接进入 Atlas 索引检索
- `target_pipeline="C"`：跳过 SkillExecutor，直接进入攻略检索

收益：省去一次 LLM 路由调用（~500ms + ~1k tokens）

### 6. 戴冠战 Skill 迁移策略

三阶段渐进迁移，不破坏现有功能：

- **Phase 1（当前）**：保留现有 coronation_knowledge/coronation_team Skill 不动，链路 C 作为新通道并行存在。路由优先匹配 coronation domain → 走现有 Skill；仅当 Skill 无结果时降级到 guide domain → 走链路 C
- **Phase 2（攻略内容充实后）**：逐步将 `config/coronation/*.json` 转为 `guides/coronation/*.md`，每转一个文件对应 Skill 的 topic 改为走 guide 检索
- **Phase 3**：coronation domain 完全并入 guide domain，coronation Skills 退役

理由：现有 Skill 的结构化 JSON 数据质量高于 Markdown 攻略，在攻略尚未全覆盖前保留 Skill 更安全。

## 方案对比记录

### 检索方案对比（链路 C）

| 维度 | 关键词(BM25) | 向量(Embedding) | LLM-as-Retriever |
|:-----|:-----------|:---------------|:----------------|
| 检索质量 | ★★☆ | ★★★ | ★★★☆ |
| Token 成本 | ★★★ | ★★☆ | ★☆☆ |
| 延迟 | ★★★ | ★★☆ | ★☆☆ |
| 实现复杂度 | ★★☆ | ★★★ | ★☆☆ |
| 戴冠战适配度 | ★★★ | ★★☆ | ★★☆ |
| 可扩展性 | ★★★ | ★★★ | ★☆☆ |

**决策**：关键词优先 + 向量兜底。戴冠战内容高度结构化，关键词命中率本身就高；向量层作为可选增强，当关键词无结果时降级触发。

### 校验方案对比（链路 B）

| 维度 | 重量级(二次LLM) | 轻量级(索引验证) |
|:-----|:--------------|:---------------|
| 延迟 | +2-5s | +<10ms |
| Token 成本 | +2k-5k | 0 |
| 准确率 | 高(但有LLM自身幻觉) | 确定性(索引不幻觉) |
| 覆盖范围 | 任何声明 | 仅限可索引事实 |

**决策**：轻量级索引验证。Atlas 有完整结构化索引，可直接验证名称/时间/关联关系，无需 LLM 介入。

## Token 成本影响评估

| 链路 | 新增 Token 消耗 | 说明 |
|:-----|:---------------|:-----|
| A | 无变化 | 现有逻辑不变 |
| B | +2k~5k/次 | Atlas 索引摘要注入 |
| C | +1k~3k/次 | 检索到的文档块注入(top_k=3) |
| 路由 | +~200/次 | 新增 atlas/guide domain 规则 |
| Preset 直连 | -1k/次 | 省去路由 LLM 调用 |

链路 C 相比现有戴冠战 Skill：结构化 JSON ~2k-4k tokens vs Markdown 文档块 ~1k-3k tokens，基本持平或略降。

## 后续行动

1. 创建 ADR 记录本决策
2. 更新 `需求描述.md` 产品路线图
3. 制定实施计划，按 Phase 1 → 2 → 3 推进
4. 更新 `docs/architecture.html` + `docs/architecture.json`
