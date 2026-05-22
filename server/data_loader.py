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
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests", file=sys.stderr)
    sys.exit(1)

API_BASE = "https://api.atlasacademy.io"
NICE_SERVANT_URL = f"{API_BASE}/export/JP/nice_servant_lang_en.json"
NICE_EQUIP_URL = f"{API_BASE}/export/JP/nice_equip_lang_en.json"
OUTPUT_DIR = Path(__file__).parent / "data"
KNOWLEDGE_DIR = Path(__file__).parent / "knowledge"
CONFIG_DIR = Path(__file__).parent / "config"

SKILL_LV10_INDEX = 9


# ============================================================
# 知识库加载
# ============================================================


def merge_effect_overlay(effects: list[dict]) -> list[dict]:
    """将 server/config/effect_overrides.json 中的业务扩展合并到效果列表。

    合并规则：
    - overlay 中的同名效果 **覆盖** 主 schema 中的定义
    - overlay 中的新效果 **追加** 到列表末尾
    - overlay 文件不存在时静默跳过（降级为纯 schema）
    """
    overlay_path = CONFIG_DIR / "effect_overrides.json"
    if not overlay_path.exists():
        return effects
    with open(overlay_path, encoding="utf-8") as f:
        overlay = json.load(f)
    overlay_effects = overlay.get("effects", [])
    if not overlay_effects:
        return effects

    existing_names = {e["name"] for e in effects}
    merged = list(effects)
    for eff in overlay_effects:
        if eff["name"] in existing_names:
            # 覆盖同名效果
            merged = [eff if e["name"] == eff["name"] else e for e in merged]
        else:
            # 追加新效果
            merged.append(eff)
    return merged


def load_effect_schema() -> dict:
    """加载 effect_schema.json 知识库（含 effects, traits, triggerBuffTypes）。

    自动合并 server/config/effect_overrides.json 中的业务扩展效果。
    """
    schema_path = KNOWLEDGE_DIR / "effect_schema.json"
    if not schema_path.exists():
        print("⚠️  effect_schema.json 不存在，请先运行 sync_chaldea.py")
        return {"effects": [], "traits": {}, "triggerBuffTypes": []}
    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)
    effects = merge_effect_overlay(data.get("effects", []))
    return {
        "effects": effects,
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


# CN 服从者数据 URL（用于获取中文技能名/宝具名）
CN_SERVANT_URL = f"{API_BASE}/export/CN/nice_servant.json"
# CN 服礼装数据 URL（用于获取中文礼装名和效果描述）
CN_EQUIP_URL = f"{API_BASE}/export/CN/nice_equip.json"
# 缓存文件路径
SKILL_NAMES_CN_CACHE = OUTPUT_DIR / "skill_names_cn.json"
CE_NAMES_CN_CACHE = OUTPUT_DIR / "ce_names_cn.json"


def load_skill_names_cn() -> dict:
    """从 Atlas API CN 服获取中文技能名/宝具名映射。

    构建 {skill_id: cn_name} 和 {td_id: cn_name} 映射表。
    优先使用本地缓存（server/data/skill_names_cn.json），缓存不存在时从远程下载。

    Returns:
        {"skills": {skill_id_str: cn_name, ...}, "tds": {td_id_str: cn_name, ...}}
    """
    # 尝试加载缓存
    if SKILL_NAMES_CN_CACHE.exists():
        with open(SKILL_NAMES_CN_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("skills") and cached.get("tds"):
            return cached

    # 从远程下载 CN 服数据
    print("   📥 从 Atlas API CN 服下载中文技能名映射...")
    try:
        resp = requests.get(CN_SERVANT_URL, timeout=120)
        resp.raise_for_status()
        cn_servants = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"   ⚠️  CN 服数据下载失败: {e}，跳过中文技能名填充")
        return {"skills": {}, "tds": {}}

    # 构建映射表
    skill_map: dict[str, str] = {}
    td_map: dict[str, str] = {}

    for svt in cn_servants:
        for sk in svt.get("skills", []):
            sk_id = sk.get("id")
            sk_name = sk.get("name", "")
            if sk_id and sk_name:
                skill_map[str(sk_id)] = sk_name
        for np in svt.get("noblePhantasms", []):
            np_id = np.get("id")
            np_name = np.get("name", "")
            if np_id and np_name:
                td_map[str(np_id)] = np_name

    result = {"skills": skill_map, "tds": td_map}

    # 写入缓存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SKILL_NAMES_CN_CACHE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"   ✅ 获取 {len(skill_map)} 个中文技能名, {len(td_map)} 个中文宝具名")

    return result


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
# 特性合并（对齐 Chaldea 羁绊加成逻辑）
# ============================================================

# Atlas API 中永久有效的 endedAt 阈值（与 Chaldea kNeverClosedTimestamp 对齐）
_NEVER_CLOSED_TIMESTAMP = 1893423600


def _merge_traits(svt: dict) -> dict:
    """合并从者的完整特性数据（对齐 Chaldea equip_bond_bonus.dart 逻辑）。

    三层叠加机制：
    1. ascensionAdd.individuality — 灵基/灵衣特性覆盖（非空时替代基础 traits）
       - ascension section: 标准灵基 0-4 的特性覆盖
       - costume section: 灵衣阶段的特性覆盖（key 为大数字如 301330）
    2. traitAdd (condType=none) — 无条件附加特性（按 limitCount 分配到对应灵基）
    3. traitAdd (condType!=none) — 有条件附加特性（存入 conditionalTraits）

    Returns:
        dict 包含：
        - "traits": list[int]  — 全灵基并集（用于通用筛选）
        - "traitsByAscension": dict[str, list[int]] | None — 按灵基的完整特性（仅灵基间有差异时）
        - "conditionalTraits": list[dict] | None — 有条件的附加特性（仅存在时）
    """
    # 1. 基础 traits
    base_traits = _trait_ids(svt.get("traits", []))

    # 2. 解析 ascensionAdd.individuality（灵基特性覆盖）
    asc_add = svt.get("ascensionAdd", {})
    indiv_data = asc_add.get("individuality", {})
    asc_indiv: dict[str, list[int]] = {}  # stage_key → trait IDs

    # Atlas API 格式: {"ascension": {"0": [...], "1": [...], ...}, "costume": {"301330": [...], ...}}
    asc_section = indiv_data.get("ascension", {})
    if isinstance(asc_section, dict):
        for stage_key, trait_list in asc_section.items():
            if trait_list:  # 只保留非空的灵基覆盖
                asc_indiv[str(stage_key)] = _trait_ids(trait_list)

    # 灵衣特性覆盖（costume section）— 灵衣阶段可能携带额外特性（如兽科从者 2821）
    costume_section = indiv_data.get("costume", {})
    if isinstance(costume_section, dict):
        for costume_key, trait_list in costume_section.items():
            if trait_list:
                asc_indiv[str(costume_key)] = _trait_ids(trait_list)

    # 3. 解析 traitAdd
    trait_add_list = svt.get("traitAdd", [])
    # 无条件附加（condType=none, eventId=0, 未过期）
    unconditional_all: list[int] = []  # limitCount=-1，全灵基
    unconditional_by_limit: dict[int, list[int]] = {}  # limitCount=N，指定灵基
    # 有条件附加
    conditional_traits: list[dict] = []

    for entry in trait_add_list:
        cond_type = entry.get("condType", "none")
        event_id = entry.get("eventId", 0)
        limit_count = entry.get("limitCount", -1)
        ended_at = entry.get("endedAt", 0)
        trait_ids = _trait_ids(entry.get("trait", []))

        if not trait_ids:
            continue

        if cond_type != "none":
            # 有条件特性（questClear / svtLimit 等）
            conditional_traits.append(
                {
                    "traitIds": trait_ids,
                    "condType": cond_type,
                    "condId": entry.get("condId", 0),
                }
            )
            continue

        # condType=none：跳过活动相关和已过期的（与 Chaldea 一致）
        if event_id != 0:
            continue
        if ended_at > 0 and ended_at < _NEVER_CLOSED_TIMESTAMP:
            continue

        if limit_count == -1:
            unconditional_all.extend(trait_ids)
        else:
            unconditional_by_limit.setdefault(limit_count, []).extend(trait_ids)

    # 4. 构建各灵基的完整特性
    # 确定所有灵基 stage keys（0-4 是标准，也可能有 costume）
    all_stages: set[str] = set()
    if asc_indiv:
        all_stages.update(asc_indiv.keys())
    if unconditional_by_limit:
        all_stages.update(str(k) for k in unconditional_by_limit.keys())
    # 如果没有任何灵基差异数据，不需要生成 traitsByAscension
    if not all_stages and not asc_indiv:
        # 简单路径：只有无条件全灵基附加
        merged_traits = sorted(set(base_traits + unconditional_all))
        result: dict = {"traits": merged_traits}
        if conditional_traits:
            result["conditionalTraits"] = conditional_traits
        return result

    # 标准灵基 0-4
    for i in range(5):
        all_stages.add(str(i))

    traits_by_ascension: dict[str, list[int]] = {}
    all_traits_union: set[int] = set()

    for stage_key in sorted(all_stages, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0)):
        # 灵基覆盖：非空则替代基础 traits，否则用基础 traits
        stage_base = asc_indiv.get(stage_key, [])
        if stage_base:
            stage_traits = list(stage_base)
        else:
            stage_traits = list(base_traits)

        # 合并无条件全灵基附加
        stage_traits.extend(unconditional_all)

        # 合并无条件指定灵基附加
        limit_key = int(stage_key) if stage_key.isdigit() else -1
        if limit_key in unconditional_by_limit:
            stage_traits.extend(unconditional_by_limit[limit_key])

        stage_traits_sorted = sorted(set(stage_traits))
        traits_by_ascension[stage_key] = stage_traits_sorted
        all_traits_union.update(stage_traits_sorted)

    # 5. 判断灵基间是否有差异
    all_same = True
    reference = traits_by_ascension.get("0", [])
    for stage_key, stage_traits in traits_by_ascension.items():
        if stage_traits != reference:
            all_same = False
            break

    result = {"traits": sorted(all_traits_union)}
    if not all_same:
        result["traitsByAscension"] = traits_by_ascension
    if conditional_traits:
        result["conditionalTraits"] = conditional_traits
    return result


# ============================================================
# 数据提取
# ============================================================


def fetch_normal_servants() -> list[dict]:
    """从 Atlas Academy API 拉取可召唤从者数据（type=normal, collectionNo>0）。"""
    print("📡 正在从 Atlas Academy API 拉取从者数据...")
    resp = requests.get(NICE_SERVANT_URL, timeout=120)
    resp.raise_for_status()
    servants = resp.json()
    normal = [s for s in servants if s.get("type") in ("normal", "heroine") and s.get("collectionNo", 0) > 0]
    print(f"   ✅ 获取到 {len(normal)} 个从者")
    return normal


def get_face_url_absolute(servant: dict) -> str:
    """获取从者头像的 Atlas CDN 绝对 URL（优先最终再临）。

    供下载脚本使用，保留原始外部 URL。
    """
    faces = servant.get("extraAssets", {}).get("faces", {}).get("ascension", {})
    if faces:
        return faces.get("4") or faces.get("3") or faces.get("2") or faces.get("1", "")
    return ""


def get_face_url(servant: dict) -> str:
    """获取从者头像的本地相对路径（优先最终再临）。

    返回 /faces/f_xxx.png 格式，前端直接使用该路径请求本域。
    """
    absolute_url = get_face_url_absolute(servant)
    if not absolute_url:
        return ""
    # 从 URL 提取文件名，如 https://static.atlasacademy.io/JP/Faces/f_1001003.png → f_1001003.png
    parsed = urlparse(absolute_url)
    filename = Path(parsed.path).name
    return f"/faces/{filename}" if filename else ""


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


def classify_target_type(func_target_type: str) -> str:
    """将 FuncTargetType 分类为简单的目标类型。

    细分单体队友（ptOne）和全体队友（party），数据层区分存储。
    """
    if func_target_type in ("self", "commandTypeSelfTreasureDevice"):
        return "self"
    # 单体队友类（指定一个队友）
    if func_target_type in (
        "ptOne",
        "ptOneOther",
        "ptOneHpLowestRate",
        "ptSelfAnotherFirst",
        "ptRandom",
    ):
        return "ptOne"
    # 全体队友类（全队含自己）
    if func_target_type in ("ptAll", "ptFull", "fieldAll"):
        return "party"
    # 全体队友（不含自己）
    if func_target_type == "ptOther":
        return "partyOther"
    if func_target_type.startswith("pt"):
        return "party"  # 未知 pt* 类型兜底
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

    同一 skillNum 可能因技能强化存在多个版本，仅保留最后出现的
    （Atlas Academy 数据中强化后版本排在后面）。

    Returns:
        (效果集合, 技能详情列表)
    """
    all_effects: set[str] = set()
    # 用 skillNum 作为 key 去重，后出现的覆盖前出现的（即最新版本）
    details_by_num: dict[int, dict] = {}

    for skill in servant.get("skills", []):
        if skill.get("type") != "active":
            continue

        skill_num = skill.get("num", 0)
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
            details_by_num[skill_num] = {
                "skillId": skill.get("id", 0),
                "skillName": skill.get("name", ""),
                "skillNum": skill_num,
                "effects": skill_effects,
            }

    return all_effects, list(details_by_num.values())


def build_database(
    servants: list[dict], matcher: dict, name_mapping: dict, skill_cn_map: dict | None = None
) -> list[dict]:
    """构建通用从者数据库。

    Args:
        servants: Atlas API 返回的从者列表
        matcher: 效果匹配索引
        name_mapping: 从者名翻译映射
        skill_cn_map: 中文技能名/宝具名映射 {"skills": {id: cn_name}, "tds": {id: cn_name}}
    """
    db = []
    total_with_effects = 0
    cn_skills = (skill_cn_map or {}).get("skills", {})
    cn_tds = (skill_cn_map or {}).get("tds", {})

    for svt in servants:
        skill_effects, skill_details = extract_skill_effects(svt, matcher)

        # 填充中文技能名（有映射用中文，无映射保留英文原名）
        for sk_detail in skill_details:
            sk_id = str(sk_detail.get("skillId", 0))
            if sk_id in cn_skills:
                sk_detail["skillName"] = cn_skills[sk_id]

        # 从 skillDetails 中计算 totalSelfCharge（自身可获得的 NP 充能总量）
        # self + party（含自己的全队）+ ptOne（可指定自身）都算入自充
        # partyOther（不含自己）不算入自充
        total_self_charge = 0
        # 同时生成 npCharges（前端卡片展示用，按技能独立条目）
        # 格式: [{skillNum, chargePercent, targetType}]
        # targetType 映射: self→self, party→ptAll, ptOne→ptOne, partyOther→ptAll
        np_charges: list[dict] = []
        _np_charge_target_map = {"self": "self", "party": "ptAll", "ptOne": "ptOne", "partyOther": "ptAll"}
        for sk in skill_details:
            for eff in sk.get("effects", []):
                if eff.get("type") != "gainNp":
                    continue
                tt = eff.get("targetType", "")
                charge_percent = eff.get("valueMax", 0) // 100  # 千分比→百分比
                if tt in ("self", "party", "ptOne"):
                    total_self_charge += charge_percent
                if charge_percent > 0:
                    np_charges.append(
                        {
                            "skillNum": sk.get("skillNum", 0),
                            "chargePercent": charge_percent,
                            "targetType": _np_charge_target_map.get(tt, "self"),
                        }
                    )

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
                            "npId": np.get("id", 0),
                            "npName": np.get("name", ""),
                            "effects": np_effect_entries,
                        }
                    )
                break

        # 填充中文宝具名（有映射用中文，无映射保留英文原名）
        for np_detail in np_details:
            np_id = str(np_detail.get("npId", 0))
            if np_id in cn_tds:
                np_detail["npName"] = cn_tds[np_id]

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
            "_faceUrlSource": get_face_url_absolute(svt),
            # Phase 3 新增属性（traits 合并 traitAdd + ascensionAdd.individuality）
            **_merge_traits(svt),
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
            "totalSelfCharge": total_self_charge,
            "npCharges": np_charges,
            "maxSelfCharge": total_self_charge,
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
        f"{sum(1 for s in db if s['totalSelfCharge'] > 0)} 个有自充, "
        f"{total_with_effects} 个有效果数据"
    )
    return db


# ============================================================
# 概念礼装数据构建
# ============================================================


def fetch_craft_essences() -> list[dict]:
    """从 Atlas Academy API 拉取全量概念礼装数据（type=servantEquip, collectionNo>0）。"""
    print("📡 正在从 Atlas Academy API 拉取概念礼装数据...")
    resp = requests.get(NICE_EQUIP_URL, timeout=180)
    resp.raise_for_status()
    equips = resp.json()
    normal = [e for e in equips if e.get("type") == "servantEquip" and e.get("collectionNo", 0) > 0]
    print(f"   ✅ 获取到 {len(normal)} 个概念礼装")
    return normal


def load_ce_names_cn() -> dict[str, str]:
    """从 Atlas API CN 服获取中文礼装名和效果描述映射。

    构建 {ce_id_str: {"name": cn_name, "skills": {skill_id_str: detail_text}}} 映射表。
    优先使用本地缓存（server/data/ce_names_cn.json）。

    Returns:
        {"id_str": {"name": "万华镜", "skills": {"990067": "自身以宝具值已达80%...", ...}}}
    """
    # 尝试加载缓存
    if CE_NAMES_CN_CACHE.exists():
        with open(CE_NAMES_CN_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached:
            return cached

    # 从远程下载 CN 服礼装数据
    print("   📥 从 Atlas API CN 服下载中文礼装名映射...")
    try:
        resp = requests.get(CN_EQUIP_URL, timeout=180)
        resp.raise_for_status()
        cn_equips = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"   ⚠️  CN 服礼装数据下载失败: {e}，跳过中文名填充")
        return {}

    # 构建映射表
    result: dict[str, dict] = {}
    for ce in cn_equips:
        ce_id = str(ce.get("id", ""))
        ce_name = ce.get("name", "")
        if not ce_id or not ce_name:
            continue
        # 提取每个 skill 的中文 detail
        skills_cn: dict[str, str] = {}
        for sk in ce.get("skills", []):
            sk_id = str(sk.get("id", ""))
            sk_detail = sk.get("detail", "")
            if sk_id and sk_detail:
                skills_cn[sk_id] = sk_detail
        result[ce_id] = {"name": ce_name, "skills": skills_cn}

    # 写入缓存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CE_NAMES_CN_CACHE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"   ✅ 获取 {len(result)} 个中文礼装名")

    return result


def _classify_ce_atk_type(atk_max: int, hp_max: int) -> str:
    """根据 atkMax/hpMax 判定礼装的 ATK/HP 类型。"""
    if atk_max > 0 and hp_max == 0:
        return "pure_atk"
    if atk_max == 0 and hp_max > 0:
        return "pure_hp"
    if atk_max > 0 and hp_max > 0:
        return "mixed"
    return "zero"


def _classify_ce_obtain(ce: dict) -> str:
    """推断礼装的获取方式。

    基于 Atlas API 的 flag 字段精确分类：
    - normal: 常驻池（permanent）
    - svtEquipFriendShip: 羁绊礼装（bond）
    - svtEquipChocolate: 情人节礼装（valentine）
    - svtEquipCampaign: 限定池/付费抽取活动（limited）
    - svtEquipEvent: 活动限定抽取池（limited）
    - svtEquipEventReward: 活动配布奖励（event）
    - svtEquipManaExchange: 稀有棱柱兑换（shop）
    - svtEquipExp: 经验值礼装（exp）
    - unknown: 归入 permanent
    """
    # 优先使用特征字段（更可靠）
    if ce.get("bondEquipOwner"):
        return "bond"

    # 基于 flag 字段精确分类
    flag = ce.get("flag", "")

    flag_mapping = {
        "svtEquipFriendShip": "bond",
        "svtEquipChocolate": "valentine",
        "svtEquipCampaign": "limited",
        "svtEquipEvent": "limited",
        "svtEquipEventReward": "event",
        "svtEquipManaExchange": "shop",
        "svtEquipExp": "exp",
        "normal": "permanent",
    }

    result = flag_mapping.get(flag)
    if result:
        return result

    # valentineScript fallback（少量情人节礼装可能 flag 不是 chocolate）
    if ce.get("valentineScript"):
        return "valentine"

    return "permanent"


def get_ce_face_url_absolute(ce: dict) -> str:
    """获取礼装头像的 Atlas CDN 绝对 URL。"""
    faces = ce.get("extraAssets", {}).get("faces", {}).get("equip", {})
    if faces:
        ce_id = str(ce.get("id", ""))
        return faces.get(ce_id, "")
    return ""


def get_ce_face_url(ce: dict) -> str:
    """获取礼装头像的本地相对路径。

    返回 /faces/f_xxx.png 格式，前端直接使用该路径请求本域。
    """
    absolute_url = get_ce_face_url_absolute(ce)
    if not absolute_url:
        return ""
    parsed = urlparse(absolute_url)
    filename = Path(parsed.path).name
    return f"/faces/{filename}" if filename else ""


def _fetch_entry_function_skills(craft_essences: list[dict]) -> dict[int, dict]:
    """扫描所有礼装收集 entryFunction 引用的子 skill id，批量请求 Atlas API 获取实际效果。

    entryFunction 是 FGO 的间接引用机制：buff.type=entryFunction 的 svals[0].Value
    存储的是一个子 skill id，实际的登场效果（如 gainStar）定义在该子 skill 中。

    Args:
        craft_essences: Atlas API 返回的全量礼装列表

    Returns:
        {sub_skill_id: {"funcType": str, "buffType": str, "value": int}}
        - funcType: 子 skill 的 funcType（如 "gainStar"）
        - buffType: 如果是 addState/addStateShort 类型，buff 的 type（如 "upAtk"）
        - value: svals[0].Value 数值
    """
    import requests

    # 收集所有 entryFunction 引用的子 skill id
    sub_skill_ids: set[int] = set()
    for ce in craft_essences:
        for skill in ce.get("skills", []):
            for func in skill.get("functions", []):
                for buff in func.get("buffs", []):
                    if buff.get("type") == "entryFunction":
                        svals = func.get("svals", [])
                        sval = svals[0] if svals else {}
                        sub_id = sval.get("Value", 0)
                        if sub_id > 0:
                            sub_skill_ids.add(sub_id)

    if not sub_skill_ids:
        return {}

    print(f"   📡 获取 {len(sub_skill_ids)} 个 entryFunction 子 skill...")
    entry_map: dict[int, dict] = {}
    for sid in sub_skill_ids:
        try:
            url = f"https://api.atlasacademy.io/nice/JP/skill/{sid}?lang=en"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            sk_data = resp.json()
            for func in sk_data.get("functions", []):
                func_type = func.get("funcType", "")
                svals = func.get("svals", [])
                sval = svals[0] if svals else {}
                value = sval.get("Value", 0)
                # 如果是 addState/addStateShort，取 buff type 作为实际效果名
                buff_type = ""
                buffs = func.get("buffs", [])
                if buffs:
                    buff_type = buffs[0].get("type", "")
                entry_map[sid] = {
                    "funcType": func_type,
                    "buffType": buff_type,
                    "value": value,
                }
                break  # 只取第一个 function
        except Exception:
            continue

    print(f"   ✅ 获取到 {len(entry_map)} 个子 skill 效果")
    return entry_map


def _extract_ce_effects(
    skills: list[dict], cond_limit_count: int, matcher: dict, entry_skill_map: dict[int, dict] | None = None
) -> tuple[list[str], list[dict]]:
    """从礼装 skills 中提取指定满破条件的效果。

    Args:
        skills: 礼装的 skills 列表（原始 Atlas API 格式）
        cond_limit_count: 0=未满破, 4=满破
        matcher: 效果匹配索引
        entry_skill_map: entryFunction 子 skill 查找表（由 _fetch_entry_function_skills 构建）

    Returns:
        (效果名列表, 效果详情列表[{name, target, value}])
    """
    effects_set: set[str] = set()
    effects_details: list[dict] = []

    for skill in skills:
        # 按 condLimitCount 筛选：0=未满破效果, 4=满破效果
        if skill.get("condLimitCount", 0) != cond_limit_count:
            continue
        # 跳过活动限定的 eventDropUp 等效果（skillNum > 1 通常是活动加成）
        # 但保留 num=1 的核心效果
        for func in skill.get("functions", []):
            func_type = func.get("funcType", "")
            # 跳过活动掉落加成
            if func_type == "eventDropUp":
                continue
            # 跳过活动绑定效果（funcGroup 含 eventId > 0 表示只对特定活动关卡生效）
            func_group = func.get("funcGroup", [])
            if func_group and any(fg.get("eventId", 0) > 0 for fg in func_group):
                continue

            # 处理 entryFunction（登场效果）：间接引用子 skill
            has_entry_function = False
            for buff in func.get("buffs", []):
                if buff.get("type") == "entryFunction" and entry_skill_map:
                    has_entry_function = True
                    svals = func.get("svals", [])
                    sval = svals[0] if svals else {}
                    sub_skill_id = sval.get("Value", 0)
                    sub_info = entry_skill_map.get(sub_skill_id)
                    if sub_info:
                        # 生成 entry_ 前缀效果名
                        sub_func_type = sub_info["funcType"]
                        sub_buff_type = sub_info["buffType"]
                        sub_value = sub_info["value"]
                        # 优先用 funcType（如 gainStar），如果是 addState 类则用 buffType
                        if sub_func_type in ("addState", "addStateShort") and sub_buff_type:
                            # 首字母大写拼接：entry + UpAtk → entryUpAtk
                            effect_name = f"entry{sub_buff_type[0].upper()}{sub_buff_type[1:]}"
                        else:
                            # 首字母大写拼接：entry + GainStar → entryGainStar
                            effect_name = f"entry{sub_func_type[0].upper()}{sub_func_type[1:]}"
                        effects_set.add(effect_name)
                        effects_details.append(
                            {
                                "name": effect_name,
                                "target": "self",
                                "value": sub_value,
                            }
                        )
                    break

            if has_entry_function:
                continue

            target_type = func.get("funcTargetType", "")
            matched = _match_func_effects(func, matcher)

            # 提取数值（礼装 svals 只有一个元素，不像从者技能有 10 级）
            svals = func.get("svals", [])
            sval = svals[0] if isinstance(svals, list) and svals else (svals if isinstance(svals, dict) else {})
            value = sval.get("Value", 0) if isinstance(sval, dict) else 0

            for effect_name in matched:
                effects_set.add(effect_name)
                effects_details.append(
                    {
                        "name": effect_name,
                        "target": classify_target_type(target_type),
                        "value": value,
                    }
                )

    return sorted(effects_set), effects_details


def _extract_ce_np_charge(skills: list[dict], cond_limit_count: int) -> int:
    """提取礼装的 NP 充能百分比（满破/未满破）。

    Returns:
        NP 充能百分比（如 100 表示 100%），无充能返回 0
    """
    for skill in skills:
        if skill.get("condLimitCount", 0) != cond_limit_count:
            continue
        for func in skill.get("functions", []):
            if func.get("funcType") == "gainNp":
                svals = func.get("svals", [])
                sval = svals[0] if isinstance(svals, list) and svals else {}
                value = sval.get("Value", 0) if isinstance(sval, dict) else 0
                if value > 0:
                    return value // 100  # 千分比 → 百分比
    return 0


# ── 效果标签简称映射（用于卡片展示） ──
_CE_EFFECT_TAG_MAP: dict[str, str] = {
    "upBuster": "B卡",
    "upArts": "A卡",
    "upQuick": "Q卡",
    "upNpdamage": "宝具威力",
    "upCriticaldamage": "暴击威力",
    "upDamage": "特攻",
    "upDropnp": "NP获得",
    "regainStar": "每回合星",
    "regainNp": "每回合NP",
    "upChagetd": "OC",
    "upGainHp": "HP回复量",
    "upStarweight": "集中度",
    "upCriticalpoint": "掉星",
    "subSelfdamage": "伤害减免",
    "avoidState": "回避",
    "invincible": "无敌",
    "guts": "毅力",
    "upAtk": "攻击力",
    "upDefence": "防御力",
    "gainHp": "HP",
    "gainStar": "暴击星",
    "addDamage": "附加伤害",
    "upCommandall": "指令卡",
    "upResistInstantdeath": "即死耐性",
    "upGrantInstantdeath": "即死率",
    "breakAvoidance": "必中",
    "pierceInvincible": "无敌贯通",
    "upTolerance": "弱化耐性",
    "upGrantstate": "弱化成功率",
    # 登场效果（entryFunction 解析）
    "entryGainStar": "登场暴击星",
    "entryUpAtk": "登场攻击力",
    "entryUpCommandall": "登场指令卡",
}

# NP 相关效果（已有黄色标签单独展示，不重复到 effectTags）
_CE_NP_EFFECTS = frozenset({"gainNp"})

# 非战斗效果（活动加成等，卡片上不展示）
_CE_SKIP_EFFECTS = frozenset(
    {
        "friendPointUp",
        "userEquipExpUp",
        "expUp",
        "eventDropUp",
        "servantFriendshipUp",
        "eventDropRateUp",
        "eventPointUp",
    }
)

# NP 百分比效果（value 是万分比，需 /100）
_CE_NP_PERCENT_EFFECTS = frozenset({"regainNp"})

# 星/HP 直接数值效果
_CE_STAR_EFFECTS = frozenset({"regainStar", "gainStar", "entryGainStar"})
_CE_HP_EFFECTS = frozenset({"gainHp", "guts"})

# 登场效果（entryFunction 解析，使用千分比格式化）
_CE_ENTRY_PERCENT_EFFECTS = frozenset({"entryUpAtk", "entryUpCommandall"})


def _build_ce_effect_tags(effect_details: list[dict]) -> list[str]:
    """从 effectDetailsLB 构建卡片展示用的效果标签简称。

    规则：
    - 排除 NP 充能（gainNp）— 已有独立黄色标签
    - 排除非战斗效果（活动加成等）
    - 箭头风格：B卡↑15%、宝具威力↑20%

    Args:
        effect_details: [{"name": "upBuster", "target": "self", "value": 150}, ...]

    Returns:
        ["B卡↑15%", "宝具威力↑20%"]
    """
    tags: list[str] = []
    for eff in effect_details:
        eff_name = eff.get("name", "")
        value = eff.get("value", 0)

        # 跳过 NP 充能和非战斗效果
        if eff_name in _CE_NP_EFFECTS or eff_name in _CE_SKIP_EFFECTS:
            continue

        # 获取简称
        label = _CE_EFFECT_TAG_MAP.get(eff_name, "")
        if not label:
            # 未映射的效果，跳过（避免暴露英文原名）
            continue

        # 格式化数值
        if value and value > 0:
            if eff_name in _CE_NP_PERCENT_EFFECTS:
                # 万分比 → 百分比
                tags.append(f"{label}↑{value // 100}%")
            elif eff_name in _CE_ENTRY_PERCENT_EFFECTS:
                # 登场百分比效果（千分比 → 百分比）
                pct = value / 10
                if pct == int(pct):
                    tags.append(f"{label}↑{int(pct)}%")
                else:
                    tags.append(f"{label}↑{pct:.1f}%")
            elif eff_name == "entryGainStar":
                # 登场暴击星：特殊格式 "登场 N暴击星"
                tags.append(f"登场{value}暴击星")
            elif eff_name in _CE_STAR_EFFECTS:
                # 每回合星/普通暴击星
                if eff_name == "regainStar":
                    tags.append(f"每回合{value}暴击星")
                else:
                    tags.append(f"{label}+{value}个")
            elif eff_name in _CE_HP_EFFECTS:
                tags.append(f"{label}+{value}")
            else:
                # 默认：千分比 → 百分比（/10）
                pct = value / 10
                # 整数显示（15.0% → 15%）
                if pct == int(pct):
                    tags.append(f"{label}↑{int(pct)}%")
                else:
                    tags.append(f"{label}↑{pct:.1f}%")
        else:
            # 无数值的效果（如回避、无敌）
            tags.append(label)

    return tags


def _build_ce_entry(ce: dict, cn_data: dict, matcher: dict, entry_skill_map: dict[int, dict] | None = None) -> dict:
    """构建单条概念礼装 MV 记录。"""
    ce_id = ce.get("id", 0)
    ce_id_str = str(ce_id)
    skills = ce.get("skills", [])

    # 中文名
    cn_info = cn_data.get(ce_id_str, {})
    name_cn = cn_info.get("name", "")
    skills_cn = cn_info.get("skills", {})

    # ATK/HP 类型
    atk_max = ce.get("atkMax", 0)
    hp_max = ce.get("hpMax", 0)
    atk_type = _classify_ce_atk_type(atk_max, hp_max)

    # 获取方式
    obtain = _classify_ce_obtain(ce)

    # 提取效果（未满破 + 满破）
    effects_list, effects_details = _extract_ce_effects(skills, 0, matcher, entry_skill_map)
    effects_lb_list, effects_lb_details = _extract_ce_effects(skills, 4, matcher, entry_skill_map)

    # 如果没有满破独立效果，fallback 到未满破效果
    if not effects_lb_list and effects_list:
        effects_lb_list = effects_list
        effects_lb_details = effects_details

    # 提取 NP 充能百分比（满破优先）
    np_charge_lb = _extract_ce_np_charge(skills, 4)
    np_charge = _extract_ce_np_charge(skills, 0)
    np_charge_percent = np_charge_lb if np_charge_lb > 0 else np_charge

    # 提取中文效果描述（从 CN 服 skill detail 获取）
    effect_desc_cn = ""
    effect_desc_cn_lb = ""
    for skill in skills:
        sk_id = str(skill.get("id", ""))
        cond = skill.get("condLimitCount", 0)
        cn_detail = skills_cn.get(sk_id, "")
        if cn_detail:
            if cond == 0 and not effect_desc_cn:
                effect_desc_cn = cn_detail
            elif cond == 4 and not effect_desc_cn_lb:
                effect_desc_cn_lb = cn_detail

    # 满破描述 fallback
    if not effect_desc_cn_lb and effect_desc_cn:
        effect_desc_cn_lb = effect_desc_cn

    # 构建效果标签简称（用于前端卡片展示）
    effect_tags = _build_ce_effect_tags(effects_lb_details)

    return {
        "id": ce_id,
        "collectionNo": ce.get("collectionNo", 0),
        "name": ce.get("name", "Unknown"),
        "nameCn": name_cn,
        "rarity": ce.get("rarity", 0),
        "cost": ce.get("cost", 0),
        "atkMax": atk_max,
        "hpMax": hp_max,
        "atkType": atk_type,
        "obtain": obtain,
        "faceUrl": get_ce_face_url(ce),
        "_faceUrlSource": get_ce_face_url_absolute(ce),
        "effects": effects_list,
        "effectsLimitBreak": effects_lb_list,
        "effectDetails": effects_details,
        "effectDetailsLB": effects_lb_details,
        "effectTags": effect_tags,
        "effectDescCn": effect_desc_cn,
        "effectDescCnLB": effect_desc_cn_lb,
        "npChargePercent": np_charge_percent,
    }


def build_ce_database(craft_essences: list[dict], matcher: dict, cn_data: dict) -> list[dict]:
    """构建概念礼装数据库。

    Args:
        craft_essences: Atlas API 返回的礼装列表
        matcher: 效果匹配索引
        cn_data: 中文礼装名映射

    Returns:
        礼装 MV 记录列表
    """
    # 预获取所有 entryFunction 引用的子 skill 数据
    entry_skill_map = _fetch_entry_function_skills(craft_essences)

    db = []
    total_with_effects = 0

    for ce in craft_essences:
        entry = _build_ce_entry(ce, cn_data, matcher, entry_skill_map)
        db.append(entry)
        if entry.get("effectsLimitBreak"):
            total_with_effects += 1

    # 按稀有度降序 → collectionNo 升序排序
    db.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))

    print(
        f"   ✅ 构建礼装数据库: {len(db)} 个礼装, "
        f"{total_with_effects} 个有效果数据, "
        f"{sum(1 for e in db if e.get('npChargePercent', 0) > 0)} 个有NP充能"
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

    # 加载中文技能名/宝具名映射
    skill_cn_map = load_skill_names_cn()

    servants = fetch_normal_servants()
    db = build_database(servants, matcher, name_mapping, skill_cn_map)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "servants_db.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f"\n📄 输出: {output_path}")

    # ── 概念礼装数据构建 ──
    print("\n" + "=" * 50)
    print("🎴 概念礼装数据构建")
    print("=" * 50)

    cn_ce_data = load_ce_names_cn()
    craft_essences = fetch_craft_essences()
    ce_db = build_ce_database(craft_essences, matcher, cn_ce_data)

    ce_output_path = OUTPUT_DIR / "craft_essences_db.json"
    with open(ce_output_path, "w", encoding="utf-8") as f:
        json.dump(ce_db, f, ensure_ascii=False, indent=2)

    print(f"\n📄 输出: {ce_output_path}")
    print("✨ 全部完成!")


if __name__ == "__main__":
    main()
