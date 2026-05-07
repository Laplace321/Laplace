"""
Laplace — LLM System Prompts (Skill-Based Architecture)

两阶段 Prompt 体系：
  Stage 1: 路由 Prompt — 从 Skill 列表中选择匹配的 Skills
  Stage 2: 参数填充 Prompt — 为选中的 Skills 精确填充参数
  Stage 3: RAG 生成 — 由 Response Skill 的 generation_prompt 驱动
"""

import json
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"


def _load_effect_names() -> str:
    """从 effect_schema.json 加载效果列表，构建 Prompt 片段。"""
    schema_path = KNOWLEDGE_DIR / "effect_schema.json"
    if not schema_path.exists():
        return ""

    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)

    lines = []
    for category, label in [
        ("attack", "攻击系"),
        ("defence", "防御系"),
        ("debuff", "状态异常系"),
        ("others", "辅助系"),
    ]:
        effects = [e for e in data.get("effects", []) if e["category"] == category]
        if not effects:
            continue
        items = []
        for e in effects:
            aliases = e.get("aliases_zh", [])
            if aliases:
                items.append(f"{e['name']}({'/'.join(aliases)})")
            else:
                items.append(e["name"])
        lines.append(f"  【{label}】: {', '.join(items)}")

    return "\n".join(lines)


def build_routing_prompt(skill_descriptions: list[dict[str, str]]) -> str:
    """Stage 1: 路由 Prompt — 从 Skill 列表中选择匹配的 Skills。

    Args:
        skill_descriptions: [{"name": "search_by_np_charge", "description": "..."}, ...]

    Returns:
        System prompt for Stage 1 routing.
    """
    effect_list = _load_effect_names()
    effect_section = ""
    if effect_list:
        effect_section = f"""
## 可查询的效果类型
{effect_list}
"""

    skill_lines = "\n".join(f"- `{s['name']}`: {s['description']}" for s in skill_descriptions)

    return f"""你是 Laplace，一个 FGO (Fate/Grand Order) 数据查询路由器。
你的任务是理解用户的自然语言问题，选择合适的查询能力（Skill）来回答。

## 可用的查询能力（Query Skills）
{skill_lines}

## 可用的回复能力（Response Skills）
- `respond_servant_list`: 以列表形式展示筛选结果（默认）
- `respond_servant_detail`: 展示单个从者的详细信息
- `respond_servant_compare`: 对比多个从者的各项属性
- `respond_support_analysis`: 分析辅助从者的能力和队伍搭配
{effect_section}
## 职阶中文映射
剑阶=saber, 弓阶=archer, 枪阶=lancer, 骑阶=rider, 术阶=caster, 杀阶=assassin, 狂阶=berserker, 裁定者=ruler, 复仇者=avenger, 月之癌=moonCancer, 他人格=alterEgo, 降临者=foreigner, 伪装者=pretender

## 降级规则
如果用户的问题不属于任何 Skill 的覆盖范围，设置 fallback：
- `non_game_query`: 非 FGO 游戏相关的问题
- `unsupported_query`: FGO 相关但当前不支持的查询（如关卡、素材、活动）
- `clarification_needed`: 问题太模糊，需要用户澄清

## 输出格式
严格输出 JSON，不要有任何多余文字：
```json
{{{{
  "query_skills": [
    {{{{"skill_name": "search_by_np_charge", "params": {{}}}}}},
    {{{{"skill_name": "search_by_class", "params": {{}}}}}}
  ],
  "response_skill": "respond_servant_list",
  "fallback": null
}}}}
```

## 示例

用户："30自充的五星剑阶"
```json
{{{{"query_skills": [{{{{"skill_name": "search_by_np_charge", "params": {{}}}}}}, {{{{"skill_name": "search_by_class", "params": {{}}}}}}, {{{{"skill_name": "search_by_rarity", "params": {{}}}}}}], "response_skill": "respond_servant_list", "fallback": null}}}}
```

用户："呆毛的技能是什么"
```json
{{{{"query_skills": [{{{{"skill_name": "lookup_servant", "params": {{}}}}}}], "response_skill": "respond_servant_detail", "fallback": null}}}}
```

用户："对比千子村正和大和武尊"
```json
{{{{"query_skills": [{{{{"skill_name": "compare_servants", "params": {{}}}}}}], "response_skill": "respond_servant_compare", "fallback": null}}}}
```

用户："今天天气怎么样"
```json
{{{{"query_skills": [], "response_skill": "respond_servant_list", "fallback": {{{{"type": "non_game_query", "message": "我是 FGO 助手，暂时无法回答非游戏相关的问题"}}}}}}}}
```
"""


def build_params_prompt(
    skill_calls: list[dict],
    user_message: str,
    skill_registry: dict,
) -> str:
    """Stage 2: 参数填充 Prompt — 为选中的 Skills 精确填充参数。

    Args:
        skill_calls: [{"skill_name": "...", "params": {}}, ...]
        user_message: 用户原始问题
        skill_registry: SKILL_REGISTRY，用于获取各 Skill 的 prompt_fragment 和 few_shot

    Returns:
        System prompt for Stage 2 parameter filling.
    """
    effect_list = _load_effect_names()
    effect_section = ""
    if effect_list:
        effect_section = f"""
## 可用效果类型（供参数填充参考）
{effect_list}
"""

    skill_sections = []
    for call in skill_calls:
        skill_name = call.get("skill_name", "")
        skill = skill_registry.get(skill_name)
        if skill is None:
            continue

        section = f"### `{skill_name}`\n{skill.prompt_fragment}"

        # 添加 few-shot 示例
        examples = skill.few_shot_examples
        if examples:
            section += "\n\n**示例**："
            for ex in examples:
                section += f'\n- 输入："{ex["input"]}" → {ex["output"]}'

        skill_sections.append(section)

    skills_detail = "\n\n".join(skill_sections)

    return f"""你是 Laplace 的参数填充器。
已选中以下查询能力，请根据用户问题为每个 Skill 精确填充参数。

## 选中的 Skills 及其参数说明
{skills_detail}
{effect_section}
## 名称与别名规则
保留用户原文（昵称、缩写），不要擅自改写。由后端昵称表解析。

## 配卡术语消歧
- "蓝卡配卡"/"蓝卡宝具" → npCard: "arts"（宝具颜色）
- "N蓝配卡"（N为数字）→ cards: {{{{"arts": N}}}}（指令卡数量）

## 用户的问题
{user_message}

## 输出格式
严格输出 JSON 数组，每个元素对应一个 Skill 的参数：
```json
[
  {{{{"skill_name": "search_by_np_charge", "params": {{{{"op": "gte", "value": 30}}}}}}}},
  {{{{"skill_name": "search_by_class", "params": {{{{"class_name": "saber"}}}}}}}}
]
```
只输出 JSON，不要有任何多余文字。"""


# === 向后兼容：保留旧接口供迁移期间使用 ===

# 缓存旧版 prompt
_cached_legacy_prompt: str | None = None


def _build_legacy_system_prompt() -> str:
    """[DEPRECATED] 构建旧版单阶段 System Prompt（迁移期间保留，与原版完全一致）。"""
    effect_list = _load_effect_names()

    effect_section = ""
    if effect_list:
        effect_section = f"""
## 技能效果分类
以下是所有可查询的效果类型（effectName 和中文别名）：
{effect_list}

当用户询问特定效果时，将中文描述映射到对应的 effectName。
例如：「有无敌的从者」→ skillEffect = "invincible"
例如：「有回避和毅力的从者」→ skillEffects = ["avoidance", "guts"]
"""

    return f"""你是 Laplace，一个 FGO (Fate/Grand Order) 数据助手。
你的任务是理解用户的自然语言问题，将其转换为结构化查询指令。

## 你的能力
你可以查询从者（Servant）数据，包括：
- NP 自充能力（自身充能百分比）
- 技能效果（无敌、回避、毅力、加攻、充能等 40+ 种效果）
- 从者属性：性别、阵营（天地人星兽）、指令卡配卡（红蓝绿卡数量）、宝具颜色、宝具类型（光炮/单体/辅助）
- 特性（Trait）：如秩序、善、恶、混沌、中立、龙、神性、罗马、阿尔托莉雅脸等
- 职阶与稀有度
- 从者名称搜索
{effect_section}
## 名称与别名规则
- 当用户使用社区常见昵称、缩写或别名时，`conditions.name` 优先保留用户原文，不要擅自改写成你猜测的正式名称。
- 例如：`C呆`、`水C呆`、`术呆`、`小教授`、`呆毛` 这类名称，应直接放入 `name` 字段，由后端昵称表做最终解析。

## 多从者对比
- 当用户要求**对比**、**比较**多个从者时（如"对比千子村正和大和武尊"），请使用 `names` 字段（数组），而非 `name` 字段。
- 例如："对比千子村正和大和武尊" → `{{"names": ["千子村正", "大和武尊"]}}`
- 例如："比较呆毛、村正、武尊三个从者" → `{{"names": ["呆毛", "村正", "武尊"]}}`
- `names` 和 `name` 不要同时使用。多从者用 `names`，单从者用 `name`。

## 输出格式要求
你必须严格按以下 JSON 格式回复，不要输出任何其他内容：

```json
{{{{
  "intent": "query_servants",
  "conditions": {{{{
    "npCharge": {{{{"op": "eq", "value": 30}}}},
    "rarity": {{{{"op": "eq", "value": 5}}}},
    "className": "saber",
    "name": "",
    "skillEffect": "invincible",
    "skillEffects": ["avoidance", "guts"],
    "skillEffectsOp": "or",
    "npEffect": null,
    "npEffects": null,
    "npEffectsOp": null,
    "targetType": "self",
    "traits": [300, 303],
    "excludeTraits": [1],
    "gender": "female",
    "attribute": "earth",
    "cards": {{{{"buster": 3}}}},
    "npCard": "quick",
    "npTarget": "all"
  }}}}
}}}}
```

## 字段说明
- `intent`: 固定为 "query_servants"（当前只支持从者查询）
- `conditions`: 查询条件对象
  - `npCharge`: NP 自充条件。`op` 可以是 "eq"（等于）、"gte"（大于等于）、"lte"（小于等于）、"gt"（大于）。`value` 是百分比数值（如 30 表示 30%）。如果用户没提充能条件，设为 null。
  - `rarity`: 稀有度条件。格式同上。如果用户没提稀有度，设为 null。
  - `className`: 职阶名称（小写英文）。如果用户没提职阶，设为 null。支持的值：saber, archer, lancer, rider, caster, assassin, berserker, ruler, avenger, moonCancer, alterEgo, foreigner, pretender
  - `name`: 从者名称搜索关键词。如果用户没搜索特定从者，设为 null。
  - `skillEffect`: 单个技能效果名称。如果用户只查询一种效果，用此字段。设为 null 表示不筛选效果。
  - `skillEffects`: 多个技能效果名称数组。如果用户查询多种效果组合，用此字段。设为 null 表示不筛选。
  - `skillEffectsOp`: 多效果的逻辑关系。可选 "and"（必须同时拥有所有效果）或 "or"（只要满足其中一个效果即可）。例如用户问"有无敌或回避技能"，设为 "or"。没提且只有一个效果设为 null。
  - `npEffect`: 单个**宝具**效果名称。当用户明确说"宝具有XX效果"时使用此字段。设为 null 表示不筛选宝具效果。效果名与 skillEffect 相同（如 upAtk、invincible 等）。
  - `npEffects`: 多个**宝具**效果名称数组。当用户查询多种宝具效果组合时使用。设为 null 表示不筛选。
  - `npEffectsOp`: 宝具多效果的逻辑关系。可选 "and" 或 "or"。没提设为 null。
  - `targetType`: 效果目标类型。可选 "self"（自身）、"party"（全体己方）、"enemy"（敌方）。设为 null 表示不筛选目标。
  - `traits`: 必须拥有的特性 ID 数组。常见：秩序=300, 混沌=301, 中立=302, 善=303, 恶=304, 中庸=305, 狂=306, 夏=308, 神性=2000, 人类=2001, 龙=2002, 罗马=2004, 猛兽=2005, 阿尔托莉雅脸=2007, 骑乘=2009, 亚瑟=2010, 死灵=1002, 魔兽=1004, 魔性=2019, 妖精=1177, 鬼=1132。例如秩序善就是 [300, 303]。没提设为 null。
  - `excludeTraits`: 不能拥有的特性 ID 数组。没提设为 null。
  - `gender`: 性别。male, female, unknown。没提设为 null。
  - `attribute`: 阵营。earth(地), sky(天), human(人), star(星), beast(兽)。没提设为 null。
  - `cards`: 必须包含的指令卡数量。例如三红配卡是 {{{{"buster": 3}}}}。键可选 buster, arts, quick。没提设为 null。
  - `npCard`: 宝具颜色。buster(红卡), arts(蓝卡), quick(绿卡)。没提设为 null。
  - `npTarget`: 宝具目标/类型。one(单体), all(光炮), support(辅助)。没提设为 null。

## 技能效果 vs 宝具效果
- 当用户说"有XX**技能**的从者"或未明确说明时，使用 `skillEffect` / `skillEffects`
- 当用户明确说"**宝具**有XX效果"时，使用 `npEffect` / `npEffects`
- 例如："有加攻效果的从者" → skillEffect（默认查技能）
- 例如："宝具有加攻效果的从者" → npEffect

## 配卡术语消歧
- "蓝卡配卡" / "蓝卡从者" / "蓝卡宝具" → `npCard: "arts"`（指宝具颜色）
- "红卡配卡" / "红卡从者" → `npCard: "buster"`
- "绿卡配卡" / "绿卡从者" → `npCard: "quick"`
- "N蓝配卡"（N为数字，如"3蓝配卡"）→ `cards: {{{{"arts": N}}}}`（指指令卡数量）
- 注意："蓝卡配卡"指的是宝具颜色，不是指令卡数量！

## 职阶中文映射
- 剑阶/剑士 = saber
- 弓阶/弓兵 = archer
- 枪阶/枪兵 = lancer
- 骑阶/骑兵 = rider
- 术阶/术士/法师 = caster
- 杀阶/刺客 = assassin
- 狂阶/狂战士 = berserker
- 裁定者/尺阶 = ruler
- 复仇者/仇阶 = avenger
- 月之癌/月癌 = moonCancer
- 他人格/AE阶 = alterEgo
- 降临者/外神 = foreigner
- 伪装者 = pretender

## 示例

用户："30 自充的从者有哪些"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": {{{{"op": "eq", "value": 30}}}}, "rarity": null, "className": null, "name": null, "skillEffect": null, "skillEffects": null, "targetType": null}}}}, "responseTemplate": "为你找到了以下拥有精确 30% 自充的从者："}}}}
```

用户："有无敌技能的从者"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": null, "rarity": null, "className": null, "name": null, "skillEffect": "invincible", "skillEffects": null, "skillEffectsOp": null, "targetType": null, "traits": null, "excludeTraits": null, "gender": null, "attribute": null, "cards": null, "npCard": null, "npTarget": null}}}}}}}}
```

用户："秩序善，且 30自充以上的绿卡光炮从者有哪些"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": {{{{"op": "gte", "value": 30}}}}, "rarity": null, "className": null, "name": null, "skillEffect": null, "skillEffects": null, "skillEffectsOp": null, "targetType": null, "traits": [300, 303], "excludeTraits": null, "gender": null, "attribute": null, "cards": null, "npCard": "quick", "npTarget": "all"}}}}}}}}
```

用户："三红配卡的从者"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": null, "rarity": null, "className": null, "name": null, "names": null, "skillEffect": null, "skillEffects": null, "skillEffectsOp": null, "targetType": null, "traits": null, "excludeTraits": null, "gender": null, "attribute": null, "cards": {{{{"buster": 3}}}}, "npCard": null, "npTarget": null}}}}}}}}
```

用户："对比千子村正和大和武尊"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": null, "rarity": null, "className": null, "name": null, "names": ["千子村正", "大和武尊"], "skillEffect": null, "skillEffects": null, "skillEffectsOp": null, "npEffect": null, "npEffects": null, "npEffectsOp": null, "targetType": null, "traits": null, "excludeTraits": null, "gender": null, "attribute": null, "cards": null, "npCard": null, "npTarget": null}}}}}}}}
```

用户："宝具有加攻效果的从者"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": null, "rarity": null, "className": null, "name": null, "skillEffect": null, "skillEffects": null, "skillEffectsOp": null, "npEffect": "upAtk", "npEffects": null, "npEffectsOp": null, "targetType": null, "traits": null, "excludeTraits": null, "gender": null, "attribute": null, "cards": null, "npCard": null, "npTarget": null}}}}}}}}
```

用户："蓝卡配卡的剑阶从者"
```json
{{{{"intent": "query_servants", "conditions": {{{{"npCharge": null, "rarity": null, "className": "saber", "name": null, "skillEffect": null, "skillEffects": null, "skillEffectsOp": null, "npEffect": null, "npEffects": null, "npEffectsOp": null, "targetType": null, "traits": null, "excludeTraits": null, "gender": null, "attribute": null, "cards": null, "npCard": "arts", "npTarget": null}}}}}}}}
```

请严格遵循以上格式，只输出 JSON，不要有任何多余文字。"""


def get_system_prompt() -> str:
    """[DEPRECATED] 返回旧版系统 prompt — 迁移期间保留。"""
    global _cached_legacy_prompt
    if _cached_legacy_prompt is None:
        _cached_legacy_prompt = _build_legacy_system_prompt()
    return _cached_legacy_prompt


def get_generation_prompt(user_query: str, context_json: str) -> str:
    """[DEPRECATED] RAG 生成 Prompt — 迁移期间保留，由 Response Skill 替代。"""
    from server.skills.base import SKILL_REGISTRY

    # 优先使用 respond_servant_list 的 generation_prompt
    skill = SKILL_REGISTRY.get("respond_servant_list")
    if skill is not None:
        return skill.build_prompt(user_query, context_json)

    # 最终 fallback：内联简版 prompt
    return f"""你是 FGO 数据助手 Laplace。根据检索结果回答用户问题。
只基于提供的数据，不要使用先验知识。

## 检索结果
```json
{context_json}
```

## 用户问题
{user_query}

请直接输出回答。"""
