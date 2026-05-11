"""
Laplace — Agent Tool Definitions

从 SKILL_REGISTRY 和领域知识生成 Chat Completions API tools JSON 定义。
每个 tool 的 description 包含足够的语义信息，让 LLM 无需 Prompt 规则即可正确映射。

格式：Chat Completions API 嵌套格式
[{"type":"function","function":{"name":"...","description":"...","parameters":{...}}}]
"""


def build_agent_tools() -> list[dict]:
    """构建 Agent 可用的 tools 定义列表（Chat Completions 嵌套格式）。

    格式：[{"type":"function","function":{"name":"...","description":"...","parameters":{...}}}]

    分为三类：
    1. 核心查询 tools（桥接 SkillExecutor）
    2. 查表 tools（本地内存，<1ms）
    3. 外部 API tools（Atlas API runtime 反查）
    """
    return [
        # ── 核心查询 ──
        _wrap(_tool_search_servants()),
        _wrap(_tool_lookup_servant()),
        _wrap(_tool_compare_servants()),
        # ── 查表 tools（本地，<1ms）──
        _wrap(_tool_list_effects()),
        _wrap(_tool_list_traits()),
        _wrap(_tool_list_classes()),
        # ── 外部 API（Atlas API，~500ms）──
        _wrap(_tool_lookup_skill_detail()),
    ]


def _wrap(func_def: dict) -> dict:
    """将扁平 function 定义包装为 Chat Completions 嵌套格式。

    输入: {"type":"function","name":"...","description":"...","parameters":{...}}
    输出: {"type":"function","function":{"name":"...","description":"...","parameters":{...}}}
    """
    return {
        "type": "function",
        "function": {
            "name": func_def["name"],
            "description": func_def["description"],
            "parameters": func_def["parameters"],
        },
    }


# ============================================================
# 各 tool 定义函数（拆分以保持可读性）
# ============================================================


def _tool_search_servants() -> dict:
    return {
        "type": "function",
        "name": "search_servants",
        "description": (
            "按条件搜索从者。支持按职阶、稀有度、效果、NP充能、宝具类型、特性等多条件组合筛选。"
            "多个条件之间是 AND 关系（同时满足）。"
            "效果名需使用英文 key（如 upAtk、invincible、damageBoost），"
            "不确定时请先调用 list_effects 查看可用效果名。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "effects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "效果名列表（英文 key）。如 ['upAtk','invincible']。"
                        "支持虚拟复合效果：damageBoost（泛用增伤）、damageShield（挡伤害）"
                    ),
                },
                "effects_op": {
                    "type": "string",
                    "enum": ["and", "or"],
                    "description": "多效果组合方式：and（全部满足）或 or（任一满足）。默认 and",
                },
                "effect_source": {
                    "type": "string",
                    "enum": ["skill", "np", "both"],
                    "description": (
                        "效果来源：skill（仅技能）、np（仅宝具）、both（默认，同时搜）。"
                        "用户说了'技能'则用 skill，说了'宝具'则用 np，否则用 both"
                    ),
                },
                "effect_target_type": {
                    "type": "string",
                    "enum": ["self", "party", "enemy"],
                    "description": (
                        "效果目标：self（自身）、party（队友/全队）、enemy（敌方）。"
                        "注意区分正向buff和负向debuff的施加方向："
                        "正向buff（如无敌贯通、攻击力提升、暴击威力提升等增益效果）"
                        "「给别人上」「给队友上」→ party，「给自己上」→ self；"
                        "负向debuff（如防御力下降、魅惑、即死等减益效果）"
                        "「给敌人上」→ enemy。"
                        "大多数情况下不需要指定此参数，仅当用户明确区分目标时才使用"
                    ),
                },
                "effect_min_value": {
                    "type": "integer",
                    "description": "效果最小数值（百分比）。如用户说'超过50%'则传 50",
                },
                "class_name": {
                    "type": "string",
                    "description": (
                        "职阶英文名。可选值：Saber, Archer, Lancer, Rider, Caster, "
                        "Assassin, Berserker, Ruler, Avenger, MoonCancer, "
                        "AlterEgo, Foreigner, Pretender, Beast, Shielder"
                    ),
                },
                "rarity": {
                    "type": "integer",
                    "description": "稀有度（0-5星）",
                },
                "rarity_op": {
                    "type": "string",
                    "enum": ["eq", "gte", "lte", "gt", "lt"],
                    "description": "稀有度比较方式：eq（等于）、gte（大于等于）等。默认 eq",
                },
                "np_charge_value": {
                    "type": "integer",
                    "description": "NP 充能量（百分比）。如'30充以上'传 30",
                },
                "np_charge_op": {
                    "type": "string",
                    "enum": ["eq", "gte", "lte", "gt", "lt"],
                    "description": "NP 充能比较方式。默认 gte（大于等于）",
                },
                "np_card": {
                    "type": "string",
                    "enum": ["arts", "buster", "quick"],
                    "description": "宝具卡色",
                },
                "np_target": {
                    "type": "string",
                    "enum": ["all", "one", "support"],
                    "description": "宝具目标：all（全体/光炮/AOE）、one（单体）、support（辅助）",
                },
                "trait_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "特性名列表（中文）。如 ['龙','王']。系统会自动将中文特性名转换为 ID 进行筛选",
                },
                "attribute": {
                    "type": "string",
                    "description": "属性筛选。支持中文或英文：天/sky、地/earth、人/human、星/star、兽/beast",
                },
            },
            "required": [],
        },
    }


def _tool_lookup_servant() -> dict:
    return {
        "type": "function",
        "name": "lookup_servant",
        "description": ("按名称查询单个从者的详细信息。支持中文名、英文名、日文名和昵称。如'查一下梅林'、'玛修的详情'"),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "从者名称（支持中/英/日/昵称）",
                },
            },
            "required": ["name"],
        },
    }


def _tool_compare_servants() -> dict:
    return {
        "type": "function",
        "name": "compare_servants",
        "description": "对比多个从者的属性和能力。如'对比村正和武尊'",
        "parameters": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要对比的从者名称列表",
                },
            },
            "required": ["names"],
        },
    }


def _tool_list_effects() -> dict:
    return {
        "type": "function",
        "name": "list_effects",
        "description": (
            "列出所有可用的效果名及其中文别名。"
            "当你不确定用户说的效果对应哪个 effect key 时，先调用此工具查表。"
            "返回格式：[{name, aliases_zh, description, composite, includes}]"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }


def _tool_list_traits() -> dict:
    return {
        "type": "function",
        "name": "list_traits",
        "description": (
            "列出所有可用的从者特性名（中文）。"
            "当你不确定用户说的特性名称时，先调用此工具查表。"
            "常见特性：龙、王、神性、人类、圆桌骑士、兽科从者等"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }


def _tool_list_classes() -> dict:
    return {
        "type": "function",
        "name": "list_classes",
        "description": "列出所有可用的职阶名称（英文 key + 中文名）",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }


def _tool_lookup_skill_detail() -> dict:
    return {
        "type": "function",
        "name": "lookup_skill_detail",
        "description": (
            "查询从者技能在特定等级的详细数值。"
            "当用户询问某个技能在非满级时的具体数值时使用。"
            "数据来自 Atlas Academy API（实时查询）。"
            "需要先通过 search_servants 或 lookup_servant 获取从者 ID"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "servant_id": {
                    "type": "integer",
                    "description": "从者 ID（collectionNo）",
                },
                "skill_index": {
                    "type": "integer",
                    "description": "技能序号（1-3，对应第1/2/3技能）",
                },
                "level": {
                    "type": "integer",
                    "description": "技能等级（1-10）",
                },
            },
            "required": ["servant_id"],
        },
    }
