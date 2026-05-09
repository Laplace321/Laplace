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


def load_effect_schema() -> dict:
    """加载 effect_schema.json 知识库（含 effects, traits, triggerBuffTypes）。"""
    schema_path = KNOWLEDGE_DIR / "effect_schema.json"
    if not schema_path.exists():
        print("⚠️  effect_schema.json 不存在，请先运行 sync_chaldea.py")
        return {"effects": [], "traits": {}, "triggerBuffTypes": []}
    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)
    return {
        "effects": data.get("effects", []),
        "traits": data.get("traits", {}),
        "triggerBuffTypes": data.get("triggerBuffTypes", []),
    }


def load_svt_names_mapping() -> dict:
    """加载 mappings.json 中的从者中文翻译。"""
    mappings_path = KNOWLEDGE_DIR / "mappings.json"
    if not mappings_path.exists():
        return {}
    with open(mappings_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("svt_names", {})


def build_effect_matcher(schema: dict) -> dict:
    """构建效果匹配索引（含 validate 规则）。

    Returns:
        {
            "funcType": {"gainNp": ["gainNp"], ...},
            "buffType": {"upAtk": ["upAtk"], ...},
            "validates": {"upArts": {"type": "buff_ckSelfIndv_contains", ...}, ...},
            "triggerBuffTypes": ["delayFunction", "deadFunction", ...],
        }
    """
    effects = schema.get("effects", [])
    func_index: dict[str, list[str]] = {}
    buff_index: dict[str, list[str]] = {}
    validates: dict[str, dict] = {}

    for effect in effects:
        name = effect["name"]
        for ft in effect.get("funcTypes", []):
            func_index.setdefault(ft, []).append(name)
        for bt in effect.get("buffTypes", []):
            buff_index.setdefault(bt, []).append(name)
        if "validate" in effect:
            validates[name] = effect["validate"]

    return {
        "funcType": func_index,
        "buffType": buff_index,
        "validates": validates,
        "triggerBuffTypes": schema.get("triggerBuffTypes", []),
    }


def _trait_ids(trait_list: list) -> list[int]:
    """从 trait 列表中提取纯 int ID。

    Atlas API 返回的 trait 可能是 dict（{"id": 3004, "name": "..."}）或纯 int，
    预消化后应为纯 int，此函数兼容两种格式。
    """
    result = []
    for t in trait_list:
        if isinstance(t, dict):
            result.append(t.get("id", 0))
        elif isinstance(t, int):
            result.append(t)
    return result


def apply_validate(func: dict, effect_name: str, matcher: dict) -> bool:
    """通用 validate 执行器：根据声明式规则判断 func 是否匹配指定效果。

    6 种规则类型：
    1. buff_ckSelfIndv_contains — buff.ckSelfIndv 包含指定 traitValue
    2. buff_ckOpIndv_contains — buff.ckOpIndv 包含指定 traitValue
    3. buff_ckOpIndv_every_not_in — buff.ckOpIndv 全部不在 traitValues 中
    4. func_vals_contains — func.vals 包含指定 traitValue
    5. buff_type_in_trigger_set — buff.type 属于 triggerBuffTypes 集合
    6. func_target_type_in — func.funcTargetType 属于 targetTypes 集合
    """
    rule = matcher["validates"].get(effect_name)
    if rule is None:
        return True  # 无 validate 规则，粗匹配即通过

    rule_type = rule["type"]
    buffs = func.get("buffs", [])

    if rule_type == "buff_ckSelfIndv_contains":
        trait_val = rule.get("traitValue")
        return any(trait_val in _trait_ids(b.get("ckSelfIndv", [])) for b in buffs)

    if rule_type == "buff_ckOpIndv_contains":
        trait_val = rule.get("traitValue")
        if not buffs:
            return False
        return trait_val in _trait_ids(buffs[0].get("ckOpIndv", []))

    if rule_type == "buff_ckOpIndv_every_not_in":
        trait_vals = set(rule.get("traitValues", []))
        if not buffs:
            return False
        ck_op = _trait_ids(buffs[0].get("ckOpIndv", []))
        return all(t not in trait_vals for t in ck_op)

    if rule_type == "func_vals_contains":
        trait_val = rule.get("traitValue")
        return trait_val in _trait_ids(func.get("vals", []))

    if rule_type == "buff_type_in_trigger_set":
        trigger_types = set(matcher.get("triggerBuffTypes", []))
        if not buffs:
            return False
        return buffs[0].get("type", "") in trigger_types

    if rule_type == "func_target_type_in":
        allowed = set(rule.get("targetTypes", []))
        return func.get("funcTargetType", "") in allowed

    return True  # 未知规则类型，默认通过


# ============================================================
# 数据提取
# ============================================================


def fetch_normal_servants() -> list[dict]:
    """从 Atlas Academy API 拉取可召唤从者数据（type=normal, collectionNo>0）。"""
    print("📡 正在从 Atlas Academy API 拉取从者数据...")
    resp = requests.get(NICE_SERVANT_URL, timeout=120)
    resp.raise_for_status()
    servants = resp.json()
    normal = [s for s in servants if s.get("type") == "normal" and s.get("collectionNo", 0) > 0]
    print(f"   ✅ 获取到 {len(normal)} 个从者")
    return normal


def get_face_url(servant: dict) -> str:
    """获取从者头像 URL（优先最终再临）。"""
    faces = servant.get("extraAssets", {}).get("faces", {}).get("ascension", {})
    if faces:
        return faces.get("4") or faces.get("3") or faces.get("2") or faces.get("1", "")
    return ""


def _digest_append_passives(raw_passives: list[dict]) -> list[dict]:
    """预消化追加被动：只保留满级数值和解锁素材，丢弃完整 functions 嵌套。"""
    result = []
    for ap in raw_passives:
        skill = ap.get("skill", {})
        funcs = skill.get("functions", [])
        # 提取满级数值（svals 最后一个元素 = Lv.10）
        max_val = None
        func_type = ""
        buff_type = ""
        if funcs:
            fn = funcs[0]
            func_type = fn.get("funcType", "")
            svals = fn.get("svals", [])
            if svals:
                max_val = svals[-1]  # 满级数值
            buffs = fn.get("buffs", [])
            if buffs:
                buff_type = buffs[0].get("type", "")
        result.append(
            {
                "num": ap.get("num"),
                "skillName": skill.get("name", ""),
                "skillId": skill.get("id"),
                "funcType": func_type,
                "buffType": buff_type,
                "maxVal": max_val,
                "unlockMaterials": ap.get("unlockMaterials", []),
            }
        )
    return result


def _digest_skills(raw_skills: list[dict]) -> list[dict]:
    """预消化技能：只保留查询相关字段，丢弃元数据。

    裁剪规则（用户逐字段确认）：
    - skill 顶层：保留 id/num/name/type/coolDown/functions
    - function 内部：保留 funcType/funcTargetType/buffs/svals（仅满级）
    - buff 内部：保留 type/name/vals/tvals
    """
    result = []
    for sk in raw_skills:
        funcs = []
        for fn in sk.get("functions", []):
            # svals 只保留满级（最后一个元素 = Lv.10）
            svals = fn.get("svals", [])
            max_sval = svals[-1] if svals else None
            # buffs 只保留核心字段
            digested_buffs = []
            for b in fn.get("buffs", []):
                digested_buff: dict = {
                    "type": b.get("type", ""),
                    "name": b.get("name", ""),
                    "vals": b.get("vals", []),
                    "tvals": b.get("tvals", []),
                }
                # validate 规则依赖的 Trait 检查字段
                if b.get("ckSelfIndv"):
                    digested_buff["ckSelfIndv"] = [
                        t.get("id", t) if isinstance(t, dict) else t for t in b["ckSelfIndv"]
                    ]
                if b.get("ckOpIndv"):
                    digested_buff["ckOpIndv"] = [t.get("id", t) if isinstance(t, dict) else t for t in b["ckOpIndv"]]
                digested_buffs.append(digested_buff)
            # func 级别的 vals（用于 subState validate）
            func_vals = fn.get("vals", [])
            digested_fn: dict = {
                "funcType": fn.get("funcType", ""),
                "funcTargetType": fn.get("funcTargetType", ""),
                "buffs": digested_buffs,
                "svals": max_sval,
            }
            if func_vals:
                digested_fn["vals"] = [t.get("id", t) if isinstance(t, dict) else t for t in func_vals]
            funcs.append(digested_fn)
        result.append(
            {
                "id": sk.get("id"),
                "num": sk.get("num", 0),
                "name": sk.get("name", ""),
                "type": sk.get("type", ""),
                "coolDown": sk.get("coolDown", []),
                "functions": funcs,
            }
        )
    return result


def _digest_noble_phantasms(raw_nps: list[dict]) -> list[dict]:
    """预消化宝具：只保留查询相关字段，保留全部 OC 数值。

    裁剪规则（用户逐字段确认）：
    - NP 顶层：保留 id/num/name/card/type/rank/npGain/individuality/functions
    - function 内部：保留 funcType/funcTargetType/buffs/svals + svals2-5（OC）
    - buff 内部：保留 type/name/vals/tvals
    """
    result = []
    for np_data in raw_nps:
        funcs = []
        for fn in np_data.get("functions", []):
            digested_buffs = []
            for b in fn.get("buffs", []):
                digested_buff: dict = {
                    "type": b.get("type", ""),
                    "name": b.get("name", ""),
                    "vals": b.get("vals", []),
                    "tvals": b.get("tvals", []),
                }
                # validate 规则依赖的 Trait 检查字段
                if b.get("ckSelfIndv"):
                    digested_buff["ckSelfIndv"] = [
                        t.get("id", t) if isinstance(t, dict) else t for t in b["ckSelfIndv"]
                    ]
                if b.get("ckOpIndv"):
                    digested_buff["ckOpIndv"] = [t.get("id", t) if isinstance(t, dict) else t for t in b["ckOpIndv"]]
                digested_buffs.append(digested_buff)
            # func 级别的 vals（用于 subState validate）
            func_vals = fn.get("vals", [])
            digested_fn: dict = {
                "funcType": fn.get("funcType", ""),
                "funcTargetType": fn.get("funcTargetType", ""),
                "buffs": digested_buffs,
                "svals": fn.get("svals", []),
            }
            if func_vals:
                digested_fn["vals"] = [t.get("id", t) if isinstance(t, dict) else t for t in func_vals]
            # 保留 OC svals2-5
            for key in ["svals2", "svals3", "svals4", "svals5"]:
                if key in fn:
                    digested_fn[key] = fn[key]
            funcs.append(digested_fn)
        result.append(
            {
                "id": np_data.get("id"),
                "num": np_data.get("num", 0),
                "name": np_data.get("name", ""),
                "card": np_data.get("card"),
                "type": np_data.get("type", ""),
                "rank": np_data.get("rank", ""),
                "npGain": np_data.get("npGain", {}),
                "individuality": np_data.get("individuality", []),
                "functions": funcs,
            }
        )
    return result


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
                    "targetType": _classify_charge_target(target_type),
                }
    return list(charge_by_skill.values())


def _classify_charge_target(func_target_type: str) -> str:
    """将充能技能的 funcTargetType 分类为三种充能类型。"""
    if func_target_type in SELF_TARGET_TYPES:
        return "self"
    if func_target_type in PARTY_TARGET_TYPES:
        return "ptAll"
    if func_target_type in SINGLE_TARGET_TYPES:
        return "ptOne"
    return "self"  # fallback


def classify_target_type(func_target_type: str) -> str:
    """将 FuncTargetType 分类为简单的目标类型。"""
    if func_target_type in ("self", "commandTypeSelfTreasureDevice"):
        return "self"
    if func_target_type.startswith("pt") or func_target_type == "fieldAll":
        return "party"
    if func_target_type.startswith("enemy"):
        return "enemy"
    return "other"


def _match_func_effects(func: dict, matcher: dict) -> set[str]:
    """对单个 func 进行粗匹配 + validate 精筛，返回通过的效果名集合。"""
    func_type = func.get("funcType", "")

    # 粗匹配：funcType → 候选效果
    candidates = set(matcher["funcType"].get(func_type, []))
    # 粗匹配：buffType → 候选效果
    for buff in func.get("buffs", []):
        buff_type = buff.get("type", "")
        candidates.update(matcher["buffType"].get(buff_type, []))

    if not candidates:
        return set()

    # validate 精筛：每个候选效果都必须通过 validate 检查
    validated: set[str] = set()
    for effect_name in candidates:
        if apply_validate(func, effect_name, matcher):
            validated.add(effect_name)

    return validated


def extract_skill_effects(servant: dict, matcher: dict) -> tuple[set[str], list[dict]]:
    """提取从者所有技能中的全部效果（使用 validate 执行器替代手写 refine）。

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

            matched_effects = _match_func_effects(func, matcher)

            raw_svals = func.get("svals", [])
            max_sval = (
                raw_svals[-1]
                if isinstance(raw_svals, list) and raw_svals
                else raw_svals
                if isinstance(raw_svals, dict)
                else {}
            )
            for effect_name in matched_effects:
                all_effects.add(effect_name)
                skill_effects.append(
                    {
                        "type": effect_name,
                        "funcType": func_type,
                        "targetType": classify_target_type(target_type),
                        "valueMax": max_sval.get("Value", 0),
                        "turn": max_sval.get("Turn", 0),
                        "count": max_sval.get("Count", 0),
                    }
                )

        if skill_effects:
            skill_details.append(
                {
                    "skillName": skill.get("name", ""),
                    "skillNum": skill.get("num", 0),
                    "effects": skill_effects,
                }
            )

    return all_effects, skill_details


def build_database(servants: list[dict], matcher: dict, name_mapping: dict) -> list[dict]:
    """构建通用从者数据库。"""
    db = []
    total_with_effects = 0

    for svt in servants:
        charges = extract_np_charges(svt)
        skill_effects, skill_details = extract_skill_effects(svt, matcher)

        # 计算 NP 充能统计（三分类：self / ptOne / ptAll）
        self_charges = [c["chargePercent"] for c in charges if c["targetType"] == "self"]
        pt_one_charges = [c["chargePercent"] for c in charges if c["targetType"] == "ptOne"]
        pt_all_charges = [c["chargePercent"] for c in charges if c["targetType"] == "ptAll"]
        max_self_charge = max(self_charges) if self_charges else 0
        max_pt_one_charge = max(pt_one_charges) if pt_one_charges else 0
        max_pt_all_charge = max(pt_all_charges) if pt_all_charges else 0
        total_charge = sum(self_charges) + sum(pt_one_charges) + sum(pt_all_charges)

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
        np_details: list[dict] = []
        for np in svt.get("noblePhantasms", []):
            if np.get("card"):
                np_card = card_map.get(str(np["card"]), "unknown")
                # 解析宝具目标与附带特效
                np_effect_entries: list[dict] = []
                for func in np.get("functions", []):
                    ftype = func.get("funcType", "")
                    func_target = func.get("funcTargetType", "")

                    # 提取宝具特效（使用 validate 执行器）
                    matched_np_effects = _match_func_effects(func, matcher)
                    np_effects_set.update(matched_np_effects)

                    # 烘焙宝具效果详情（OC1 Lv1 = svals[0]）
                    raw_svals = func.get("svals", [])
                    lv1_sval = (
                        raw_svals[0]
                        if isinstance(raw_svals, list) and raw_svals
                        else raw_svals
                        if isinstance(raw_svals, dict)
                        else {}
                    )
                    for eff_name in matched_np_effects:
                        np_effect_entries.append(
                            {
                                "type": eff_name,
                                "funcType": ftype,
                                "targetType": classify_target_type(func_target),
                                "valueLv1": lv1_sval.get("Value", 0),
                                "turn": lv1_sval.get("Turn", 0),
                                "count": lv1_sval.get("Count", 0),
                            }
                        )

                    if "damage" in ftype.lower():
                        target = func_target
                        if target == "enemyAll":
                            np_target = "all"
                        elif target == "enemy":
                            np_target = "one"
                        else:
                            np_target = "support"

                # 如果是纯辅助宝具（没有伤害函数）
                if np_target == "unknown":
                    np_target = "support"

                if np_effect_entries:
                    np_details.append(
                        {
                            "npName": np.get("name", ""),
                            "effects": np_effect_entries,
                        }
                    )
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
            "type": svt.get("type", "normal"),
            "cost": svt.get("cost", 0),
            "atkMax": svt.get("atkMax", 0),
            "hpMax": svt.get("hpMax", 0),
            "starAbsorb": svt.get("starAbsorb", 0),
            "instantDeathChance": svt.get("instantDeathChance", 0),
            "hitsDistribution": svt.get("hitsDistribution", {}),
            "faceUrl": get_face_url(svt),
            # Phase 3 新增属性
            "traits": [t["id"] for t in svt.get("traits", [])],
            "gender": svt.get("gender", "unknown"),
            "attribute": svt.get("attribute", "unknown"),
            "cards": cards_count,
            "npCard": np_card,
            "npTarget": np_target,
            # 原始嵌套数据（物理层，预消化）
            "skills": _digest_skills(svt.get("skills", [])),
            "classPassive": svt.get("classPassive", []),
            "appendPassive": _digest_append_passives(svt.get("appendPassive", [])),
            "noblePhantasms": _digest_noble_phantasms(svt.get("noblePhantasms", [])),
            # 素材
            "ascensionMaterials": svt.get("ascensionMaterials", {}),
            "skillMaterials": svt.get("skillMaterials", {}),
            "appendSkillMaterials": svt.get("appendSkillMaterials", {}),
            "costumeMaterials": svt.get("costumeMaterials", {}),
            # Materialized Views（预计算）
            "npCharges": charges,
            "maxSelfCharge": max_self_charge,
            "maxPtOneCharge": max_pt_one_charge,
            "maxPtAllCharge": max_pt_all_charge,
            "totalCharge": total_charge,
            "hasNpCharge": bool(self_charges) or bool(pt_one_charges) or bool(pt_all_charges),
            "skillEffects": sorted(list(skill_effects)),
            "npEffects": sorted(list(np_effects_set)),
            "skillDetails": skill_details,
            "npDetails": np_details,
        }
        db.append(entry)
        if skill_effects:
            total_with_effects += 1

    print(
        f"   ✅ 构建数据库: {len(db)} 个从者, "
        f"{sum(1 for s in db if s['hasNpCharge'])} 个有自充, "
        f"{total_with_effects} 个有效果数据"
    )
    return db


def main():
    print("=" * 50)
    print("🔮 Laplace — Data Loader v2.0")
    print("=" * 50)

    # 加载效果知识库
    print("\n📚 加载知识库...")
    schema = load_effect_schema()
    name_mapping = load_svt_names_mapping()
    effects = schema["effects"]
    if effects:
        matcher = build_effect_matcher(schema)
        validate_count = len(matcher.get("validates", {}))
        print(f"   ✅ 加载 {len(effects)} 个效果分类 ({validate_count} 个含 validate 规则)")
    else:
        matcher = {"funcType": {}, "buffType": {}, "validates": {}, "triggerBuffTypes": []}
        print("   ⚠️  无效果知识库，仅提取 NP 充能数据")
    print(f"   ✅ 加载 {len(name_mapping)} 个多语言名字翻译")

    servants = fetch_normal_servants()
    db = build_database(servants, matcher, name_mapping)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "servants_db.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n📄 输出: {output_path}")
    print("✨ 完成!")


if __name__ == "__main__":
    main()
