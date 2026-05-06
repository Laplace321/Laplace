#!/usr/bin/env python3
"""
Laplace — 通用从者数据加载器

从 Atlas Academy API 拉取全量从者数据，
基于 effect_schema.json 知识库提取所有技能效果，
生成通用数据库供 Query Executor 使用。
"""

import json
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests", file=sys.stderr)
    sys.exit(1)

API_BASE = "https://api.atlasacademy.io"
NICE_SERVANT_URL = f"{API_BASE}/export/JP/nice_servant_lang_en.json"
OUTPUT_DIR = Path(__file__).parent / "data"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"

# NP 充能相关的 funcType
GAIN_NP_FUNC_TYPES = {"gainNp"}

# 自充目标类型
SELF_TARGET_TYPES = {"self"}
# 全体充能目标类型（包含自身）
PARTY_TARGET_TYPES = {"ptAll"}
# 单体他充（可指定自身）
SINGLE_TARGET_TYPES = {"ptOne"}
# 所有包含自身的目标类型
ALL_SELF_TARGETS = SELF_TARGET_TYPES | PARTY_TARGET_TYPES | SINGLE_TARGET_TYPES

SKILL_LV10_INDEX = 9


# ============================================================
# 知识库加载
# ============================================================

def load_effect_schema() -> list[dict]:
    """加载 effect_schema.json 知识库。"""
    schema_path = KNOWLEDGE_DIR / "effect_schema.json"
    if not schema_path.exists():
        print("⚠️  effect_schema.json 不存在，请先运行 sync_chaldea.py")
        return []
    with open(schema_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("effects", [])

def load_svt_names_mapping() -> dict:
    """加载 mappings.json 中的从者中文翻译。"""
    mappings_path = KNOWLEDGE_DIR / "mappings.json"
    if not mappings_path.exists():
        return {}
    with open(mappings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("svt_names", {})


def build_effect_matcher(effects: list[dict]) -> dict:
    """
    构建效果匹配索引。

    Returns:
        {
            "funcType": {"gainNp": ["gainNp"], "gainStar": ["gainStar"], ...},
            "buffType": {"upAtk": ["upAtk"], "invincible": ["invincible"], ...},
        }
    """
    func_index: dict[str, list[str]] = {}
    buff_index: dict[str, list[str]] = {}

    for effect in effects:
        name = effect["name"]
        for ft in effect.get("funcTypes", []):
            func_index.setdefault(ft, []).append(name)
        for bt in effect.get("buffTypes", []):
            buff_index.setdefault(bt, []).append(name)

    return {"funcType": func_index, "buffType": buff_index}


# ============================================================
# 数据提取
# ============================================================

def fetch_servants() -> list[dict]:
    """从 Atlas Academy API 拉取全量从者数据。"""
    print("📡 正在从 Atlas Academy API 拉取从者数据...")
    resp = requests.get(NICE_SERVANT_URL, timeout=120)
    resp.raise_for_status()
    servants = resp.json()
    normal = [
        s for s in servants
        if s.get("type") == "normal" and s.get("collectionNo", 0) > 0
    ]
    print(f"   ✅ 获取到 {len(normal)} 个从者")
    return normal


def get_face_url(servant: dict) -> str:
    """获取从者头像 URL（优先最终再临）。"""
    faces = servant.get("extraAssets", {}).get("faces", {}).get("ascension", {})
    if faces:
        return faces.get("4") or faces.get("3") or faces.get("2") or faces.get("1", "")
    return ""


def extract_np_charges(servant: dict) -> list[dict]:
    """提取从者所有技能中的 NP 充能效果。

    同一 skillNum 可能因技能强化存在多个版本，仅保留最后出现的
    （Atlas Academy 数据中强化后版本排在后面）。
    待办：不同服务器多版本技能的选择逻辑需要产品设计。
    """
    # 用 skillNum 作为 key 去重，后出现的覆盖前出现的（即最新版本）
    charge_by_skill: dict[int, dict] = {}
    for skill in servant.get("skills", []):
        if skill.get("type") != "active":
            continue
        skill_num = skill.get("num", 0)
        for func in skill.get("functions", []):
            if func.get("funcType") not in GAIN_NP_FUNC_TYPES:
                continue
            target_type = func.get("funcTargetType", "")
            if target_type not in ALL_SELF_TARGETS:
                continue
            svals = func.get("svals", [])
            if len(svals) < 10:
                continue
            lv10_value = svals[SKILL_LV10_INDEX].get("Value", 0)
            if lv10_value > 0:
                charge_by_skill[skill_num] = {
                    "skillName": skill.get("name", ""),
                    "skillNum": skill_num,
                    "chargePercent": lv10_value // 100,
                    "chargeValue": lv10_value,
                    "targetType": "self" if target_type in SELF_TARGET_TYPES else "party",
                }
    return list(charge_by_skill.values())


def classify_target_type(func_target_type: str) -> str:
    """将 FuncTargetType 分类为简单的目标类型。"""
    if func_target_type in ("self", "commandTypeSelfTreasureDevice"):
        return "self"
    if func_target_type.startswith("pt") or func_target_type == "fieldAll":
        return "party"
    if func_target_type.startswith("enemy"):
        return "enemy"
    return "other"


def refine_card_effects(func: dict, matched_effects: set[str]) -> set[str]:
    """根据 Buff 名称精确过滤卡色魔放，防止通用枚举导致的污染。"""
    buffs = func.get("buffs", [])
    has_generic_buff = any(b.get("type") in ("upCommandall", "upCommandatk", "upCommandstar", "upCommandnp") for b in buffs)
    
    if not has_generic_buff:
        return matched_effects
        
    # 如果当前集合中有卡色效果，则根据 buff name 进行二次判定
    card_effects = {"upArts", "upQuick", "upBuster"}
    if not (matched_effects & card_effects):
        return matched_effects
        
    refined = set()
    for eff in matched_effects:
        if eff in card_effects:
            for b in buffs:
                b_name = b.get("name", "").lower()
                # 显式包含颜色名称才保留
                if eff == "upArts" and "arts" in b_name: refined.add(eff)
                if eff == "upQuick" and "quick" in b_name: refined.add(eff)
                if eff == "upBuster" and "buster" in b_name: refined.add(eff)
                # 如果是真正的三色提升 (Command Performance Up)
                if "command" in b_name and ("performance" in b_name or "up" in b_name) and "arts" not in b_name and "quick" not in b_name and "buster" not in b_name:
                    refined.add(eff)
        else:
            refined.add(eff)
    return refined


def extract_skill_effects(
    servant: dict, matcher: dict
) -> tuple[set[str], list[dict]]:
    """
    提取从者所有技能中的全部效果。

    Returns:
        (效果集合, 技能详情列表)
    """
    all_effects: set[str] = set()
    skill_details: list[dict] = []

    for skill in servant.get("skills", []):
        if skill.get("type") != "active":
            continue

        skill_effects: list[dict] = []
        for func in skill.get("functions", []):
            func_type = func.get("funcType", "")
            target_type = func.get("funcTargetType", "")

            # 通过 funcType 匹配效果
            matched_effects = set(matcher["funcType"].get(func_type, []))

            # 通过 buffType 匹配效果（addState 系列函数）
            for buff in func.get("buffs", []):
                buff_type = buff.get("type", "")
                matched_effects.update(matcher["buffType"].get(buff_type, []))

            # 二次精炼：防止卡色污染
            matched_effects = refine_card_effects(func, matched_effects)

            for effect_name in matched_effects:
                all_effects.add(effect_name)
                skill_effects.append({
                    "type": effect_name,
                    "funcType": func_type,
                    "targetType": classify_target_type(target_type),
                })

        if skill_effects:
            skill_details.append({
                "skillName": skill.get("name", ""),
                "skillNum": skill.get("num", 0),
                "effects": skill_effects,
            })

    return all_effects, skill_details


def build_database(servants: list[dict], matcher: dict, name_mapping: dict) -> list[dict]:
    """构建通用从者数据库。"""
    db = []
    total_with_effects = 0

    for svt in servants:
        charges = extract_np_charges(svt)
        skill_effects, skill_details = extract_skill_effects(svt, matcher)

        # 计算 NP 充能统计
        self_charges = [c["chargePercent"] for c in charges if c["targetType"] == "self"]
        party_charges = [c["chargePercent"] for c in charges if c["targetType"] == "party"]
        max_self_charge = max(self_charges) if self_charges else 0
        max_party_charge = max(party_charges) if party_charges else 0
        total_self_charge = sum(self_charges) + sum(party_charges)

        # 计算卡色构成
        cards_count = {"arts": 0, "buster": 0, "quick": 0}
        card_map = {"1": "arts", "2": "buster", "3": "quick"}
        for c in svt.get("cards", []):
            if str(c) in card_map:
                cards_count[card_map[str(c)]] += 1

        # 解析宝具信息
        np_card = "unknown"
        np_target = "unknown"
        np_effects_set = set()
        for np in svt.get("noblePhantasms", []):
            if np.get("card"):
                np_card = card_map.get(str(np["card"]), "unknown")
                # 解析宝具目标与附带特效
                for func in np.get("functions", []):
                    ftype = func.get("funcType", "")
                    
                    # 提取宝具特效 (复用技能提取逻辑)
                    matched_effects = set(matcher["funcType"].get(ftype, []))
                    for buff in func.get("buffs", []):
                        buff_type = buff.get("type", "")
                        matched_effects.update(matcher["buffType"].get(buff_type, []))
                    
                    # 二次精炼：防止卡色污染
                    matched_effects = refine_card_effects(func, matched_effects)
                    
                    np_effects_set.update(matched_effects)

                    if "damage" in ftype.lower():
                        target = func.get("funcTargetType", "")
                        if target == "enemyAll":
                            np_target = "all"
                        elif target == "enemy":
                            np_target = "one"
                        else:
                            np_target = "support"
                
                # 如果是纯辅助宝具（没有伤害函数）
                if np_target == "unknown":
                    np_target = "support"
                break

        # 获取原名与中文翻译
        original_name = svt.get("originalName", "")
        alias_cn = ""
        if original_name in name_mapping:
            alias_cn = name_mapping[original_name].get("CN", "")

        entry = {
            "id": svt["id"],
            "collectionNo": svt.get("collectionNo", 0),
            "name": svt.get("name", "Unknown"),
            "originalName": original_name,
            "aliasCN": alias_cn,
            "rarity": svt.get("rarity", 0),
            "className": svt.get("className", "unknown"),
            "faceUrl": get_face_url(svt),
            # Phase 3 新增属性
            "traits": [t["id"] for t in svt.get("traits", [])],
            "gender": svt.get("gender", "unknown"),
            "attribute": svt.get("attribute", "unknown"),
            "cards": cards_count,
            "npCard": np_card,
            "npTarget": np_target,
            # NP 充能数据（向后兼容）
            "npCharges": charges,
            "maxSelfCharge": max_self_charge,
            "maxPartyCharge": max_party_charge,
            "totalSelfCharge": total_self_charge,
            "hasNpCharge": bool(self_charges) or bool(party_charges) or len(charges) > 0,
            "skillEffects": sorted(list(skill_effects)),
            "npEffects": sorted(list(np_effects_set)),
            "skillDetails": skill_details
        }
        db.append(entry)
        if skill_effects:
            total_with_effects += 1

    print(f"   ✅ 构建数据库: {len(db)} 个从者, "
          f"{sum(1 for s in db if s['hasNpCharge'])} 个有自充, "
          f"{total_with_effects} 个有效果数据")
    return db


def main():
    print("=" * 50)
    print("🔮 Laplace — Data Loader v2.0")
    print("=" * 50)

    # 加载效果知识库
    print("\n📚 加载知识库...")
    effects = load_effect_schema()
    name_mapping = load_svt_names_mapping()
    if effects:
        matcher = build_effect_matcher(effects)
        print(f"   ✅ 加载 {len(effects)} 个效果分类")
    else:
        matcher = {"funcType": {}, "buffType": {}}
        print("   ⚠️  无效果知识库，仅提取 NP 充能数据")
    print(f"   ✅ 加载 {len(name_mapping)} 个多语言名字翻译")

    servants = fetch_servants()
    db = build_database(servants, matcher, name_mapping)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "servants_db.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n📄 输出: {output_path}")
    print("✨ 完成!")


if __name__ == "__main__":
    main()
