"""
Laplace — LLM Prompts

Skill-Based Architecture 的 Prompt 定义：
- RAG 生成 Prompt（get_generation_prompt）
- Stage 1 路由 Prompt（build_routing_prompt）
- Stage 2 参数精填 Prompt（build_params_prompt）
"""

import json
from pathlib import Path

_effect_hints_cache: str | None = None


def _load_effect_hints() -> str:
    """从 effect_schema.json 加载效果语义描述，生成 Prompt 注入段。

    格式：effectName: 中文名 — 语义描述
    仅包含有 description 的效果，按 category 分组。
    """
    global _effect_hints_cache
    if _effect_hints_cache is not None:
        return _effect_hints_cache

    schema_path = Path(__file__).parent / "knowledge" / "effect_schema.json"
    if not schema_path.exists():
        _effect_hints_cache = ""
        return ""

    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)

    lines: list[str] = []
    for effect in data.get("effects", []):
        desc = effect.get("description", "")
        aliases = effect.get("aliases_zh", [])
        if not desc:
            continue
        zh_name = aliases[0] if aliases else effect["name"]
        # 将全部俗称展示给 LLM，用 / 分隔，确保路由层能识别玩家用语
        if len(aliases) > 1:
            aka = " / ".join(aliases)
            lines.append(f"- `{effect['name']}`: {aka} — {desc}")
        else:
            lines.append(f"- `{effect['name']}`: {zh_name} — {desc}")

    if lines:
        _effect_hints_cache = "\n".join(lines)
    else:
        _effect_hints_cache = ""
    return _effect_hints_cache


def get_generation_prompt(user_query: str, context_json: str) -> str:
    """
    第二阶段：RAG 生成阶段的 Prompt。
    基于后端检索到的数据，要求大模型生成对用户的最终回复。
    """
    return f"""你是一个智能、友好的 FGO 游戏数据助手 Laplace。
用户向你提出了一个问题，系统已经在数据库中检索到了相关数据。
请你根据传入的【检索结果上下文】，直接回答用户的问题。

## 你的原则
1. **直接回答问题**：如果用户问“某某从者的自充是多少”，不要回答“为你找到了以下从者”，而是直接说“某某从者的最大自充是 30%”。
2. **结合全局统计（绝对纪律）**：你的回答必须基于上下文中提供的 `total_found` 数字。即使 `top_results_details` 里只提供了 5 位代表，你也**必须**报出完整的总数。例如说：“根据你的条件，为你找到了 {{total_found}} 位从者，其中包括以下代表：...”。**绝不允许**将代表数量当做总数！如果 `total_found` 为 0，委婉地告诉用户没有找到匹配的从者。
3. **绝不瞎编（禁绝先验知识）**：你的回答必须**完全且仅能**基于【检索结果上下文】中提供的数据。
    *   **禁止脑补**：严禁使用你自身内部关于 FGO 从者的任何先验知识。即使你“知道”某个从者有红魔放，如果上下文中没有，也绝对不能写。
    *   **色卡强化严谨性**：如果从者的数据（skillEffects/npEffects）中没有明确提到某色卡（如红卡/Buster）的性能提升，**严禁**在总结中提到该色卡的强化。不要因为从者有蓝绿魔放就习惯性地脑补成“三色魔放”。    *   **只列正面能力**：保持回复自然，仅列出从者"有"的能力。**严禁**列出"无某某能力"等负向事实，除非用户明确询问。4. **简洁明快**：保持对话简短，不需要列出所有从者的每一个属性，只需要回答用户关心的问题即可。
5. **格式规范**：优先使用 Markdown 列表和粗体突出关键数据。
6. **合理分类**：
   - `skillEffects`: 从者的主动技能效果。
   - `npEffects`: 从者的宝具附带效果（如降防、特攻、无敌贯通等）。
   - `totalCharge`: 从者的总充能量（含自充 + 他充 + 群充，均可为自身充能）。请根据 npCharges 中的 targetType 区分描述："self" → 自充（仅给自己）、"ptOne" → 他充（给指定一个队友，也可选自己）、"ptAll" → 群充（全队含自己）。请自然地描述充能能力，准确区分充能类型。
7. **能力边界（绝对纪律）**：你当前只具备从者查询、从者筛选（按职阶/稀有度/属性/效果/配卡/特性/充能）、从者对比、辅助分析的能力。**严禁**主动提议或暗示你能做队伍搭配推荐、礼装推荐、关卡攻略、素材规划、抽卡建议等尚未实现的功能。不要在回复末尾添加"需要我帮你做XX吗？"之类的引导语，除非用户明确询问你能做什么。

## 检索结果上下文
```json
{context_json}
```

## 用户的问题
{user_query}

请直接输出你的回答文案。严禁包含任何 JSON、代码块标签、元说明、免责声明或数据来源注释（如"以上信息基于数据生成"之类的话）。你的回复应该像一个真人助手在对话，不要暴露任何系统实现细节。
"""


# ============================================================
# Stage 1 路由 Prompt（Skill-Based Architecture, ADR-018）
# ============================================================


def build_routing_prompt(skill_descriptions: list[dict[str, str]]) -> str:
    """构建 Stage 1 路由 Prompt。

    Args:
        skill_descriptions: [{"name": "search_by_class", "description": "按职阶筛选"}, ...]

    Returns:
        系统 Prompt 字符串
    """
    skills_section = "\n".join(f"- `{s['name']}`: {s['description']}" for s in skill_descriptions)

    # 动态加载效果语义描述
    effect_hints = _load_effect_hints()
    effect_section = ""
    if effect_hints:
        effect_section = f"""
## 效果语义参考（用于 search_by_skill_effect / search_by_np_effect）
当用户查询涉及技能效果时，请将自然语言映射到以下效果 key：
{effect_hints}
"""

    return f"""你是 Laplace 路由器。根据用户的自然语言问题，选择需要执行的 Skill 组合。

## 可用 Skills
{skills_section}
{effect_section}
## 可用 Response Skills
- `respond_servant_list`: 以列表形式展示筛选到的从者（默认）
- `respond_servant_detail`: 展示单个从者的详细信息
- `respond_servant_compare`: 对比多个从者并给出分析
- `respond_support_analysis`: 分析辅助从者的能力并推荐搭配

## 输出格式
严格按以下 JSON 格式输出，不要有任何其他内容：

```json
{{
  "skill_calls": [
    {{"skill_name": "search_by_class", "params": {{"className": "Caster"}}}},
    {{"skill_name": "search_by_rarity", "params": {{"op": "eq", "value": 5}}}}
  ],
  "response_skill": "respond_servant_list",
  "fallback": null
}}
```

## 路由规则
1. 将用户问题拆解为一个或多个 Skill 调用，多个 Skill 表示 AND 组合筛选
2. `params` 中的字段名必须与 Skill 定义的参数名完全一致
3. 单从者查询用 `lookup_servant`，多从者对比用 `compare_servants`
4. 如果用户的问题无法匹配任何 Skill，设置 fallback：
   ```json
   {{"skill_calls": [], "response_skill": "respond_servant_list", "fallback": {{"code": "no_match", "message": "无法理解你的问题"}}}}
   ```
5. 根据查询类型选择合适的 response_skill

## 示例

用户："30自充以上的Caster"
```json
{{"skill_calls": [{{"skill_name": "search_by_np_charge", "params": {{"op": "gte", "value": 30}}}}, {{"skill_name": "search_by_class", "params": {{"className": "Caster"}}}}], "response_skill": "respond_servant_list"}}
```

用户："查一下梅林"
```json
{{"skill_calls": [{{"skill_name": "lookup_servant", "params": {{"name": "梅林"}}}}], "response_skill": "respond_servant_detail"}}
```

用户："对比村正和武尊"
```json
{{"skill_calls": [{{"skill_name": "compare_servants", "params": {{"names": ["村正", "武尊"]}}}}], "response_skill": "respond_servant_compare"}}
```
"""


def build_params_prompt(
    skill_calls: list[dict],
    user_message: str,
    skill_registry: dict,
) -> str:
    """构建 Stage 2 参数精填 Prompt。

    当 Stage 1 路由的参数不够精确时，可通过 Stage 2 让 LLM 补充参数细节。

    Args:
        skill_calls: Stage 1 输出的 SkillCall 列表
        user_message: 用户原始消息
        skill_registry: SKILL_REGISTRY

    Returns:
        系统 Prompt 字符串
    """
    calls_desc = []
    for call in skill_calls:
        skill_name = call.get("skill_name", "")
        params = call.get("params", {})
        skill = skill_registry.get(skill_name)
        if skill is None:
            continue
        schema_info = ""
        if hasattr(skill, "params_schema") and skill.params_schema is not None:
            schema_info = f"\n    参数 Schema: {skill.params_schema.model_json_schema()}"
        calls_desc.append(f"  - Skill: `{skill_name}` — {skill.description}\n    当前参数: {params}{schema_info}")

    calls_section = "\n".join(calls_desc) if calls_desc else "  （无）"

    return f"""你是 Laplace 参数精填器。Stage 1 路由已选定了以下 Skills，请根据用户原始问题补充或修正参数。

## 用户原始问题
{user_message}

## Stage 1 路由结果
{calls_section}

## 输出格式
以 JSON 数组格式输出修正后的 skill_calls，格式与 Stage 1 相同：
```json
[
  {{"skill_name": "xxx", "params": {{...}}}}
]
```

只输出 JSON 数组，不要有任何其他内容。
"""
