"""
Laplace — Agent Tool Handlers

桥接 Agent Loop 和现有 SkillExecutor / 数据层。
每个 handler 接收 Agent 参数 dict，返回结果 dict。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from server.data_loader import load_effect_schema
from server.skills.executor import ExecutionResult, SkillExecutor

# ── 常量 ──
ATLAS_API_BASE = "https://api.atlasacademy.io"
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"

# ── 单例 ──
_executor = SkillExecutor()


# ============================================================
# 核心查询 handlers
# ============================================================


def handle_search_servants(params: dict) -> dict:
    """搜索从者 — 将 Agent 参数映射为 SkillCall 列表，调用 SkillExecutor。"""
    skill_calls: list[dict] = []

    # 1. 效果筛选
    effects = params.get("effects") or []
    if effects:
        skill_calls.append(
            {
                "skill_name": "search_by_effect",
                "params": {
                    "effects": effects,
                    "effectsOp": params.get("effects_op", "and"),
                    "source": params.get("effect_source", "both"),
                    "targetType": params.get("effect_target_type"),
                    "minValue": params.get("effect_min_value"),
                    "maxValue": params.get("effect_max_value"),
                },
            }
        )

    # 2. 职阶筛选
    class_name = params.get("class_name")
    if class_name:
        skill_calls.append(
            {
                "skill_name": "search_by_class",
                "params": {"className": class_name},
            }
        )

    # 3. 稀有度筛选
    rarity = params.get("rarity")
    if rarity is not None:
        skill_calls.append(
            {
                "skill_name": "search_by_rarity",
                "params": {
                    "value": rarity,
                    "op": params.get("rarity_op", "eq"),
                },
            }
        )

    # 4. NP 充能筛选
    np_charge = params.get("np_charge_value")
    if np_charge is not None:
        skill_calls.append(
            {
                "skill_name": "search_by_np_charge",
                "params": {
                    "value": np_charge,
                    "op": params.get("np_charge_op", "gte"),
                },
            }
        )

    # 5. 宝具卡色 / 宝具目标筛选（合并为一个 SkillCall）
    np_card = params.get("np_card")
    np_target = params.get("np_target")
    if np_card or np_target:
        cards_params: dict[str, Any] = {}
        if np_card:
            cards_params["npCard"] = np_card
        if np_target:
            cards_params["npTarget"] = np_target
        skill_calls.append(
            {
                "skill_name": "search_by_cards",
                "params": cards_params,
            }
        )

    # 6. 特性筛选（中文特性名，由 search_by_traits Skill 内部做名称→ID 转换）
    trait_names = params.get("trait_names")
    if trait_names:
        skill_calls.append(
            {
                "skill_name": "search_by_traits",
                "params": {"traitNames": trait_names},
            }
        )

    # 7. 属性筛选（中文→英文映射，MV 中是英文小写）
    attribute = params.get("attribute")
    if attribute:
        attr_zh_to_en = {
            "天": "sky",
            "地": "earth",
            "人": "human",
            "星": "star",
            "兽": "beast",
        }
        attribute_en = attr_zh_to_en.get(attribute, attribute.lower())
        skill_calls.append(
            {
                "skill_name": "search_by_attribute",
                "params": {"attribute": attribute_en},
            }
        )

    # 无任何条件 → 返回提示
    if not skill_calls:
        return {
            "total": 0,
            "servants": [],
            "message": "未指定任何搜索条件，请提供职阶、效果、稀有度等筛选条件。",
        }

    result: ExecutionResult = _executor.execute(skill_calls)

    # 构建精简返回（避免 token 浪费）
    top_servants = result.servants[:10]
    summary_list = []
    for s in top_servants:
        summary_list.append(
            {
                "id": s.get("collectionNo"),
                "name": s.get("aliasCN") or s.get("name"),
                "class": s.get("className"),
                "rarity": s.get("rarity"),
            }
        )

    response: dict[str, Any] = {
        "total": result.total_found,
        "top_results": summary_list,
        "message": result.fallback_message if result.is_fallback else None,
        # 完整从者数据供前端卡片渲染（Agent Loop 会 pop 掉，不传给 LLM）
        "_full_servants": top_servants,
    }
    return response


def handle_lookup_servant(params: dict) -> dict:
    """查询单个从者详情。"""
    name = params.get("name", "")
    if not name:
        return {"error": "缺少从者名称"}

    result: ExecutionResult = _executor.execute(
        [{"skill_name": "lookup_servant", "params": {"name": name}}],
        response_skill_name="respond_servant_detail",
    )

    if result.is_fallback or not result.servants:
        return {"error": f"未找到名为「{name}」的从者", "total": 0}

    servant = result.servants[0]
    detail = _build_servant_detail(servant)
    # 完整从者数据供前端卡片渲染（Agent Loop 会 pop 掉，不传给 LLM）
    detail["_full_servants"] = [servant]
    return detail


def handle_compare_servants(params: dict) -> dict:
    """对比多个从者。"""
    names = params.get("names", [])
    if len(names) < 2:
        return {"error": "至少需要两个从者名称进行对比"}

    result: ExecutionResult = _executor.execute(
        [{"skill_name": "compare_servants", "params": {"names": names}}],
        response_skill_name="respond_servant_compare",
    )

    if result.is_fallback or not result.servants:
        return {"error": "未找到匹配的从者", "total": 0}

    details = [_build_servant_detail(s) for s in result.servants]
    return {
        "total": len(details),
        "servants": details,
        # 完整从者数据供前端卡片渲染（Agent Loop 会 pop 掉，不传给 LLM）
        "_full_servants": result.servants,
    }


# ============================================================
# 查表 handlers（本地内存，<1ms）
# ============================================================


def handle_list_effects(_params: dict) -> dict:
    """列出所有可用效果名及中文别名。"""
    schema = load_effect_schema()
    effects = schema.get("effects", [])

    result = []
    for eff in effects:
        entry: dict[str, Any] = {
            "name": eff["name"],
            "aliases_zh": eff.get("aliases_zh", []),
        }
        if eff.get("composite"):
            entry["composite"] = True
            entry["includes"] = eff.get("includes", [])
        result.append(entry)

    return {"total": len(result), "effects": result}


def handle_list_traits(_params: dict) -> dict:
    """列出所有可用的从者特性名（中文）。"""
    mappings_path = KNOWLEDGE_DIR / "mappings.json"
    if not mappings_path.exists():
        return {"total": 0, "traits": []}

    with open(mappings_path, encoding="utf-8") as f:
        data = json.load(f)

    traits_raw = data.get("traits", {})
    result = []
    for tid, names in traits_raw.items():
        cn = names.get("CN", "")
        if cn:
            result.append({"id": int(tid), "name_cn": cn})

    return {"total": len(result), "traits": result}


def handle_list_classes(_params: dict) -> dict:
    """列出所有可用的职阶名称（从 config/translations.json 读取）。"""
    translations_path = Path(__file__).parent.parent / "config" / "translations.json"
    if not translations_path.exists():
        return {"total": 0, "classes": []}

    with open(translations_path, encoding="utf-8") as f:
        data = json.load(f)

    class_map = data.get("className", {})
    classes = [{"key": key, "name_cn": name_cn} for key, name_cn in class_map.items()]
    return {"total": len(classes), "classes": classes}


# ============================================================
# 外部 API handler（Atlas API，~500ms）
# ============================================================


async def handle_lookup_skill_detail(params: dict) -> dict:
    """从 Atlas Academy API 查询从者技能各等级详细数值。

    用于当 MV (servants_db.json) 中不存在的低频数据（如 Lv6 技能效果）。

    注意：tool 定义中 servant_id 指的是 collectionNo（对用户可见的编号），
    但 Atlas API 路径需要内部 id（如 100100）。需要从本地 MV 做映射。
    """
    collection_no = params.get("servant_id")
    if collection_no is None:
        return {"error": "缺少 servant_id 参数"}

    skill_index = params.get("skill_index")  # 1-based, 可选
    level = params.get("level")  # 1-10, 可选

    # collectionNo → 内部 id 映射（Atlas API 路径需要内部 id）
    from server.query_executor import load_database

    db = load_database()
    atlas_id = None
    for s in db:
        if s.get("collectionNo") == collection_no:
            atlas_id = s.get("id")
            break

    if atlas_id is None:
        # fallback: 直接用 collectionNo 尝试（可能适用于某些端点）
        atlas_id = collection_no

    # 从 Atlas API 获取从者完整数据
    url = f"{ATLAS_API_BASE}/nice/JP/servant/{atlas_id}?lore=false&lang=en"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"error": f"Atlas API 请求失败: {e}"}

    # 提取技能信息
    skills = data.get("skills", [])
    # 过滤掉被强化替换的旧技能（只保留最新版本）
    active_skills = [s for s in skills if s.get("num", 0) > 0]
    # 按 num 分组，每组取 priority 最高的
    skill_map: dict[int, dict] = {}
    for s in active_skills:
        num = s.get("num", 0)
        if num not in skill_map or s.get("priority", 0) > skill_map[num].get("priority", 0):
            skill_map[num] = s
    ordered_skills = [skill_map[n] for n in sorted(skill_map.keys())]

    if skill_index is not None:
        # 返回指定技能
        if skill_index < 1 or skill_index > len(ordered_skills):
            return {"error": f"技能序号 {skill_index} 超出范围（共 {len(ordered_skills)} 个技能）"}
        return _extract_skill_detail(ordered_skills[skill_index - 1], level)

    # 返回所有技能概要
    result = []
    for i, skill in enumerate(ordered_skills, 1):
        detail = _extract_skill_detail(skill, level)
        detail["skill_index"] = i
        result.append(detail)

    return {
        "servant_name": data.get("name", ""),
        "collection_no": data.get("collectionNo"),
        "skills": result,
    }


# ============================================================
# 内部辅助函数
# ============================================================


def _extract_skill_detail(skill: dict, level: int | None) -> dict:
    """从 Atlas API 原始技能数据中提取精简信息。"""
    result: dict[str, Any] = {
        "name": skill.get("name", ""),
        "cooldown": skill.get("coolDown", []),
    }

    functions = skill.get("functions", [])
    effects = []
    for func in functions:
        func_type = func.get("funcType", "")
        buffs = func.get("buffs", [])
        buff_type = buffs[0].get("type", "") if buffs else ""

        svals = func.get("svals", [])
        # 提取各等级数值
        values = []
        for sv in svals:
            val = sv.get("Value", 0)
            values.append(val)

        effect_info: dict[str, Any] = {
            "funcType": func_type,
            "buffType": buff_type,
            "target": func.get("funcTargetType", ""),
        }

        if level is not None and 1 <= level <= len(values):
            effect_info["value_at_level"] = values[level - 1]
            effect_info["value_percent"] = round(values[level - 1] / 10, 1)
        else:
            effect_info["values_all_levels"] = values
            if values:
                effect_info["values_percent"] = [round(v / 10, 1) for v in values]

        effects.append(effect_info)

    result["effects"] = effects
    return result


def _build_servant_detail(servant: dict) -> dict:
    """从 MV 数据构建从者详情返回。

    MV 字段参照 servants_db.json 实际结构：
    - skillDetails[].skillName / .skillNum / .effects[]
    - npDetails[].npName / .effects[]
    - 顶级: npCard, npTarget, className (小写)
    """
    detail: dict[str, Any] = {
        "id": servant.get("collectionNo"),
        "name": servant.get("aliasCN") or servant.get("name"),
        "name_en": servant.get("name"),
        "class": servant.get("className"),
        "rarity": servant.get("rarity"),
        "hp_max": servant.get("hpMax"),
        "atk_max": servant.get("atkMax"),
    }

    # 技能概要（字段名: skillName, skillNum, effects）
    skills = servant.get("skillDetails", [])
    if skills:
        detail["skills"] = []
        for i, sk in enumerate(skills, 1):
            sk_info: dict[str, Any] = {
                "index": i,
                "name": sk.get("skillName", ""),
                "skill_num": sk.get("skillNum", i),
            }
            effects = sk.get("effects", [])
            if effects:
                sk_info["effects"] = [
                    {
                        "name": e.get("name", ""),
                        "target": e.get("target", ""),
                    }
                    for e in effects
                ]
            detail["skills"].append(sk_info)

    # 宝具概要（字段名: npName, effects; 卡色/目标在顶级字段）
    np_list = servant.get("npDetails", [])
    if np_list:
        detail["noble_phantasm"] = []
        for n in np_list:
            np_info: dict[str, Any] = {
                "name": n.get("npName", ""),
                "card": servant.get("npCard", ""),
                "target": servant.get("npTarget", ""),
            }
            effects = n.get("effects", [])
            if effects:
                np_info["effects"] = [
                    {
                        "name": e.get("name", ""),
                        "target": e.get("target", ""),
                    }
                    for e in effects
                ]
            detail["noble_phantasm"].append(np_info)

    return detail


# ── Handler 注册表 ──
TOOL_HANDLERS: dict[str, Any] = {
    "search_servants": handle_search_servants,
    "lookup_servant": handle_lookup_servant,
    "compare_servants": handle_compare_servants,
    "list_effects": handle_list_effects,
    "list_traits": handle_list_traits,
    "list_classes": handle_list_classes,
    "lookup_skill_detail": handle_lookup_skill_detail,
}
