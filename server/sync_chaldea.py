#!/usr/bin/env python3
"""
Laplace — Schema Mirror 知识库同步脚本

从 Chaldea Dart 源码中提取 FGO 领域知识（枚举、效果分类、职阶映射），
生成 JSON 知识库文件供 LLM System Prompt 和 QueryExecutor 使用。

用法：
    python3 server/sync_chaldea.py

设计原则：
    - 纯正则解析，不依赖 Dart SDK
    - 幂等操作，重复运行覆盖旧文件
    - 生成 _meta.json 追踪版本
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# === 路径配置 ===
PROJECT_ROOT = Path(__file__).parent.parent
_default_chaldea = str(PROJECT_ROOT / "chaldea-center" / "chaldea")
CHALDEA_ROOT = Path(os.getenv("CHALDEA_SRC_PATH", _default_chaldea))
GAMEDATA_DIR = CHALDEA_ROOT / "lib" / "models" / "gamedata"
OUTPUT_DIR = Path(__file__).parent / "knowledge"

# Chaldea 源码仓库地址
CHALDEA_REPO_URL = "https://github.com/chaldea-center/chaldea.git"

# Dart 源文件路径
FUNC_DART = GAMEDATA_DIR / "func.dart"
BUFF_DART = GAMEDATA_DIR / "buff.dart"
EFFECT_DART = GAMEDATA_DIR / "effect.dart"
COMMON_DART = GAMEDATA_DIR / "common.dart"


# ============================================================
# 1. 枚举解析器
# ============================================================


def parse_dart_enum(file_path: Path, enum_name: str) -> list[dict]:
    """
    解析 Dart 枚举定义，提取 name(value) 格式。

    支持格式：
        enumValue(42),
        enumValue(42, '别名'),

    Returns:
        [{"name": "saber", "value": 1}, ...]
    """
    content = file_path.read_text(encoding="utf-8")

    # 找到 enum 块
    pattern = rf"enum\s+{enum_name}\s*\{{(.*?)\n\s*(?:final|const|static|;)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        print(f"  ⚠️  未找到 enum {enum_name} in {file_path.name}")
        return []

    enum_body = match.group(1)

    # 匹配每个枚举值：name(intValue) 或 name(intValue, 'str')
    entries = []
    for m in re.finditer(
        r"(\w+)\s*\(\s*(-?\d+)(?:\s*,\s*['\"]([^'\"]*)['\"])?"
        r"(?:\s*,\s*(-?\d+))?\s*\)",
        enum_body,
    ):
        name = m.group(1)
        value = int(m.group(2))
        label = m.group(3)  # 可选的短标签（如 SvtClass 的 '剣'）
        extra = int(m.group(4)) if m.group(4) else None

        entry = {"name": name, "value": value}
        if label:
            entry["label"] = label
        if extra is not None:
            entry["baseClassId"] = extra
        entries.append(entry)

    return entries


# ============================================================
# 1b. Trait 常量提取（供 validate 规则使用）
# ============================================================

# validate 逻辑依赖的 Trait 常量名列表
VALIDATE_TRAIT_NAMES = {
    "cardArts",
    "cardBuster",
    "cardQuick",
    "buffPositiveEffect",
    "buffNegativeEffect",
    "buffIncreaseDamage",
}


def extract_validate_traits(common_dart: Path) -> dict[str, int]:
    """从 common.dart 的 Trait 枚举中提取 validate 依赖的常量值。"""
    entries = parse_dart_enum(common_dart, "Trait")
    traits = {}
    for entry in entries:
        if entry["name"] in VALIDATE_TRAIT_NAMES:
            traits[entry["name"]] = entry["value"]
    missing = VALIDATE_TRAIT_NAMES - set(traits.keys())
    if missing:
        print(f"  ⚠️  未找到 Trait 常量: {missing}")
    return traits


# ============================================================
# 1c. kBuffValueTriggerTypes 提取（供 triggerFunc validate 使用）
# ============================================================


def extract_trigger_buff_types(buff_dart: Path) -> list[str]:
    """从 buff.dart 提取 kBuffValueTriggerTypes 包含的 BuffType 列表。"""
    content = buff_dart.read_text(encoding="utf-8")
    # 找到 kBuffValueTriggerTypes 的定义块
    start_match = re.search(r"kBuffValueTriggerTypes\s*=\s*\(\)\s*\{", content)
    if not start_match:
        print("  ⚠️  未找到 kBuffValueTriggerTypes 定义")
        return []

    # 提取所有 BuffType.xxx 引用
    # 从定义开始到闭合的 }();
    start = start_match.start()
    depth = 0
    end = start
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    block = content[start:end]
    buff_types = re.findall(r"BuffType\.(\w+)", block)
    # 去重
    return sorted(set(buff_types))


# ============================================================
# 2. SkillEffect 分类解析器
# ============================================================


def _extract_block(content: str, start: int) -> str:
    """从 start 位置开始，提取到匹配的闭合括号 ); 的完整代码块。"""
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "(":
            depth += 1
        elif content[i] == ")":
            depth -= 1
            if depth == 0:
                return content[start : i + 1]
    return content[start:]


def _parse_validate_rule(block: str) -> dict | None:
    """从 SkillEffect 构造块中解析 validate lambda，转换为声明式 JSON 规则。

    支持 5 种模式：
    1. buff.ckSelfIndv.contains(Trait.xxx.value) → buff_ckSelfIndv_contains
    2. buff.ckOpIndv.contains(Trait.xxx.value) → buff_ckOpIndv_contains
    3. buff.ckOpIndv.every(... ![...].contains(trait)) → buff_ckOpIndv_every_not_in
    4. func.vals.contains(Trait.xxx.value) → func_vals_contains
    5. kBuffValueTriggerTypes.containsKey(func.buffs.first.type) → buff_type_in_trigger_set
    """
    if "validate:" not in block:
        return None

    # 提取 validate: 后面的 lambda 代码
    val_match = re.search(r"validate:\s*\(func\)\s*=>(.*?)(?:,\s*\)|$)", block, re.DOTALL)
    if not val_match:
        return None
    val_body = val_match.group(1).strip()

    # 模式 5: kBuffValueTriggerTypes.containsKey
    if "kBuffValueTriggerTypes.containsKey" in val_body:
        return {"type": "buff_type_in_trigger_set"}

    # 模式 4: func.vals.contains(Trait.xxx.value)
    m = re.search(r"func\.vals\.contains\(Trait\.(\w+)\.value\)", val_body)
    if m:
        return {"type": "func_vals_contains", "trait": m.group(1)}

    # 模式 3: buff.ckOpIndv.every(... ![Trait.x, Trait.y].contains(trait))
    if "ckOpIndv.every" in val_body and "!" in val_body:
        traits = re.findall(r"Trait\.(\w+)(?:\.value)?", val_body)
        if traits:
            return {"type": "buff_ckOpIndv_every_not_in", "traits": traits}

    # 模式 2: buff.ckOpIndv.contains(Trait.xxx.value)
    m = re.search(r"ckOpIndv\.contains\(Trait\.(\w+)\.value\)", val_body)
    if m:
        return {"type": "buff_ckOpIndv_contains", "trait": m.group(1)}

    # 模式 1: buff.ckSelfIndv.contains(Trait.xxx.value)
    m = re.search(r"ckSelfIndv\.contains\(Trait\.(\w+)\.value\)", val_body)
    if m:
        return {"type": "buff_ckSelfIndv_contains", "trait": m.group(1)}

    return None


def parse_effect_schema(file_path: Path) -> list[dict]:
    """从 effect.dart 提取 SkillEffect 静态字段定义（含 validate 规则）。

    统一解析所有三种构造模式：
        SkillEffect._buff('name', BuffType.xxx, validate: ...)
        SkillEffect._func('name', FuncType.xxx, validate: ...)
        SkillEffect('name', funcTypes: [...], buffTypes: [...], validate: ...)
    """
    content = file_path.read_text(encoding="utf-8")

    # 解析分类列表
    categories = {}
    for cat_name, cat_label in [
        ("kAttack", "attack"),
        ("kDefence", "defence"),
        ("kDebuffRelated", "debuff"),
        ("kOthers", "others"),
    ]:
        pattern = rf"static\s+List<SkillEffect>\s+{cat_name}\s*=\s*\[(.*?)\];"
        match = re.search(pattern, content, re.DOTALL)
        if match:
            members = re.findall(r"\b(\w+)\b", match.group(1))
            for m in members:
                categories[m] = cat_label

    effects = []
    seen_names: set[str] = set()

    # 统一匹配所有 static SkillEffect xxx = SkillEffect... 定义
    for m in re.finditer(
        r"static\s+SkillEffect\s+(\w+)\s*=\s*SkillEffect",
        content,
    ):
        var_name = m.group(1)
        block = _extract_block(content, m.start())

        # 跳过被注释掉的定义
        line_start = content.rfind("\n", 0, m.start()) + 1
        line_prefix = content[line_start : m.start()].strip()
        if line_prefix.startswith("//"):
            continue

        # 提取 effectType（第一个字符串参数）
        name_match = re.search(r"['\"](\w+)['\"]", block)
        if not name_match:
            continue
        effect_name = name_match.group(1)

        # 去重
        if effect_name in seen_names:
            continue
        seen_names.add(effect_name)

        # 判断构造模式并提取 funcTypes/buffTypes
        func_types: list[str] = []
        buff_types: list[str] = []

        if "._buff(" in block:
            # SkillEffect._buff('name', BuffType.xxx, ...)
            bt_match = re.search(r"BuffType\.(\w+)", block)
            if bt_match:
                buff_types = [bt_match.group(1)]
        elif "._func(" in block:
            # SkillEffect._func('name', FuncType.xxx, ...)
            ft_match = re.search(r"FuncType\.(\w+)", block)
            if ft_match:
                func_types = [ft_match.group(1)]
        else:
            # SkillEffect('name', funcTypes: [...], buffTypes: [...], ...)
            ft_match = re.search(r"funcTypes:\s*\[(.*?)\]", block, re.DOTALL)
            if ft_match:
                func_types = re.findall(r"FuncType\.(\w+)", ft_match.group(1))
            bt_match = re.search(r"buffTypes:\s*\[(.*?)\]", block, re.DOTALL)
            if bt_match:
                buff_types = re.findall(r"BuffType\.(\w+)", bt_match.group(1))

        # 构建效果条目
        entry: dict = {
            "name": effect_name,
            "category": categories.get(var_name, "unknown"),
            "funcTypes": func_types,
            "buffTypes": buff_types,
        }

        # 解析 validate 规则
        validate_rule = _parse_validate_rule(block)
        if validate_rule is not None:
            entry["validate"] = validate_rule

        effects.append(entry)

    return effects


# 效果语义描述（供 LLM 理解自然语言查询时做语义匹配）
EFFECT_DESCRIPTIONS: dict[str, str] = {
    "damageNpSP": "宝具附带特攻倍率，对特定特性敌人造成额外伤害",
    "upAtk": "提升攻击力，增加所有攻击的基础伤害",
    "upQuick": "提升Quick卡(绿卡)的性能，增加Quick指令卡伤害、NP获取和产星",
    "upArts": "提升Arts卡(蓝卡)的性能，增加Arts指令卡伤害和NP获取",
    "upBuster": "提升Buster卡(红卡)的性能，增加Buster指令卡伤害",
    "upDamage": "对特定特性敌人造成额外伤害的特攻效果",
    "addDamage": "攻击时附加固定数值的额外伤害",
    "upCriticaldamage": "提升暴击时的伤害倍率",
    "upCriticalpoint": "提升暴击星的掉落率，增加产星能力",
    "upStarweight": "提升暴击星的集中度，使该从者更容易获得暴击星",
    "gainStar": "立即获得一定数量的暴击星",
    "regainStar": "每回合自动获得一定数量的暴击星",
    "upNpdamage": "提升宝具的伤害倍率",
    "gainNp": "立即增加NP量，可用于自充或给队友充能",
    "regainNp": "每回合自动恢复一定NP量",
    "upDropnp": "提升攻击时的NP获取量",
    "upChagetd": "提升宝具的Overcharge阶段",
    "breakAvoidance": "攻击必定命中，无视回避状态",
    "pierceInvincible": "攻击无视无敌状态",
    "pierceDefence": "攻击无视防御力",
    "upDefence": "提升防御力，减少受到的伤害",
    "subSelfdamage": "减少受到的固定数值伤害",
    "avoidance": "闪避/回避，免疫一次攻击伤害",
    "invincible": "无敌，免疫所有伤害，持续一定回合",
    "guts": "毅力/战续，HP归零时以一定HP复活",
    "upHate": "提升目标集中度，吸引敌方攻击(嘲讽)",
    "downCriticalRateDamageTaken": "降低被敌方暴击的概率",
    "gainHp": "立即恢复HP",
    "upGainHp": "提升HP恢复效果的回复量",
    "regainHp": "每回合自动恢复一定HP",
    "addMaxhp": "增加最大HP上限",
    "reduceHp": "造成持续性HP减少(毒/诅咒/灼烧)",
    "upTolerance": "提升对弱体(负面)状态的抵抗率",
    "avoidStateNegative": "完全免疫弱体(负面)状态",
    "upGrantstate": "提升状态付与的成功率",
    "upGrantstatePositive": "提升强化状态付与的成功率",
    "upGrantstateNegative": "提升弱体(负面)状态付与的成功率",
    "upReceivePositiveEffect": "提升己方被强化的成功率",
    "subState": "解除目标身上的状态",
    "subStatePositive": "解除目标身上的正面(强化)状态",
    "subStateNegative": "解除己方身上的负面(弱体)状态",
    "upToleranceSubstate": "提升对强化解除效果的抵抗率",
    "instantDeath": "有概率即死消灭目标",
    "upResistInstantdeath": "提升对即死效果的抵抗率",
    "upGrantInstantdeath": "提升即死效果的成功率",
    "avoidInstantdeath": "完全免疫即死效果",
    "shortenSkill": "缩减技能的冷却回合数",
    "fieldIndividuality": "变更战场的场地特性",
    "friendPointUp": "提升友情点的获取量",
    "expUp": "提升御主经验值的获取量",
    "userEquipExpUp": "提升魔术礼装经验值的获取量",
    "servantFriendshipUp": "提升从者羁绊经验的获取量",
    "qpUp": "提升QP(游戏货币)的获取量",
    "eventDropUp": "提升活动素材的掉落数量",
    "triggerFunc": "在特定条件下自动发动的被动技能效果",
}

# 玩家常用俗称（Chaldea 不提供，需手动维护）
# 这些是 FGO 中文社区的惯用简称，不随 Chaldea 更新变化
PLAYER_SLANG_ZH: dict[str, list[str]] = {
    "upAtk": ["加攻"],
    "upQuick": ["绿卡提升", "绿卡增伤", "Q卡提升", "Q卡增伤", "绿魔放"],
    "upArts": ["蓝卡提升", "蓝卡增伤", "A卡提升", "A卡增伤", "蓝魔放"],
    "upBuster": ["红卡提升", "红卡增伤", "B卡提升", "B卡增伤", "红魔放"],
    "upDamage": ["特攻伤害"],
    "upCriticaldamage": ["暴击伤害", "爆伤"],
    "upCriticalpoint": ["产星", "出星"],
    "upStarweight": ["集星"],
    "gainStar": ["给星", "产星"],
    "regainStar": ["出星状态"],
    "upNpdamage": ["宝威", "宝伤"],
    "gainNp": ["自充", "充能", "群充"],
    "regainNp": ["缓充"],
    "upDropnp": ["黄金律"],
    "upChagetd": ["OC提升"],
    "upDefence": ["加防"],
    "subSelfdamage": ["减伤"],
    "guts": ["战续", "根性"],
    "upHate": ["集火"],
    "gainHp": ["回血"],
    "regainHp": ["每回合回复"],
    "upTolerance": ["异常耐性"],
    "avoidStateNegative": ["异常无效"],
    "subState": ["强化解除"],
    "subStateNegative": ["弱体解除"],
    "shortenSkill": ["减CD"],
    "triggerFunc": ["条件发动"],
}


def download_enum_translations() -> dict[str, dict[str, str]]:
    """从 Chaldea Data 下载 enums.json 中的翻译映射。

    Returns:
        {"effect_type": {...}, "buff_type": {...}, "func_type": {...}}
    """
    url = "https://raw.githubusercontent.com/chaldea-center/chaldea-data/main/mappings/enums.json"
    print("   ⬇️ 下载 enums.json（effectType/buffType/funcType 翻译）...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
            return {
                "effect_type": data.get("effect_type", {}),
                "buff_type": data.get("buff_type", {}),
                "func_type": data.get("func_type", {}),
            }
    except Exception as e:
        print(f"   ⚠️ 下载 enums.json 失败: {e}")
        return {"effect_type": {}, "buff_type": {}, "func_type": {}}


def _resolve_effect_translation(
    effect: dict,
    enum_translations: dict[str, dict[str, str]],
) -> list[str]:
    """根据 Chaldea transl getter 逻辑，为单个效果解析中文翻译。

    优先级：
    1. effect_type[effectName] — Chaldea 自定义翻译（16 个带 validate 的效果）
    2. buff_type[firstBuffType] — BuffType 翻译（大多数效果）
    3. func_type[firstFuncType] — FuncType 翻译
    4. fallback 空列表
    """
    name = effect["name"]
    aliases: list[str] = []

    # 优先级 1: effect_type 翻译
    et = enum_translations.get("effect_type", {})
    if name in et:
        cn = et[name].get("CN")
        if cn:
            aliases.append(cn)

    # 优先级 2: buff_type 翻译（如果 effect_type 没有）
    if not aliases:
        bt_map = enum_translations.get("buff_type", {})
        buff_types = effect.get("buffTypes", [])
        if buff_types and buff_types[0] in bt_map:
            cn = bt_map[buff_types[0]].get("CN")
            if cn:
                aliases.append(cn)

    # 优先级 3: func_type 翻译
    if not aliases:
        ft_map = enum_translations.get("func_type", {})
        func_types = effect.get("funcTypes", [])
        if func_types and func_types[0] in ft_map:
            cn = ft_map[func_types[0]].get("CN")
            if cn:
                aliases.append(cn)

    # 追加玩家俗称
    slang = PLAYER_SLANG_ZH.get(name, [])
    for s in slang:
        if s not in aliases:
            aliases.append(s)

    return aliases


def _add_chinese_aliases(
    effects: list[dict],
    enum_translations: dict[str, dict[str, str]] | None = None,
) -> None:
    """为效果列表添加中文别名和语义描述。"""
    if enum_translations is None:
        enum_translations = {"effect_type": {}, "buff_type": {}, "func_type": {}}
    for effect in effects:
        effect["aliases_zh"] = _resolve_effect_translation(effect, enum_translations)
        desc = EFFECT_DESCRIPTIONS.get(effect["name"])
        if desc:
            effect["description"] = desc


# ============================================================
# 3. Chaldea 源码管理
# ============================================================


def _ensure_chaldea_source() -> None:
    """确保 Chaldea 源码可用：不存在则 clone，已存在则 pull。"""
    if GAMEDATA_DIR.exists():
        print("   🔄 Chaldea 源码已存在，执行 git pull...")
        try:
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=str(CHALDEA_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                print(f"   ✅ 更新成功: {result.stdout.strip()}")
            else:
                print("   ⚠️  git pull 失败（可能有本地修改），继续使用现有版本")
        except subprocess.TimeoutExpired:
            print("   ⚠️  git pull 超时，继续使用现有版本")
        return

    print(f"   📥 Chaldea 源码不存在，正在克隆到 {CHALDEA_ROOT}...")
    CHALDEA_ROOT.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", CHALDEA_REPO_URL, str(CHALDEA_ROOT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            print(f"   ❌ git clone 失败: {result.stderr.strip()}")
            sys.exit(1)
        print("   ✅ 克隆完成")
    except subprocess.TimeoutExpired:
        print("   ❌ git clone 超时，请检查网络连接")
        sys.exit(1)


# ============================================================
# 4. 元数据生成
# ============================================================


def get_chaldea_commit() -> str:
    """获取 Chaldea 仓库的当前 commit hash。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(CHALDEA_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip()[:12]
    except Exception:
        return "unknown"


def download_mapping_data(filename: str) -> dict:
    """从 Chaldea Data 下载多语言映射字典。"""
    url = f"https://raw.githubusercontent.com/chaldea-center/chaldea-data/main/mappings/{filename}"
    print(f"   ⬇️ 下载 {filename}...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"   ⚠️ 下载 {filename} 失败: {e}")
        return {}


# ============================================================
# 5. 主流程
# ============================================================


def main():
    print("=" * 55)
    print("🔮 Laplace — Schema Mirror Sync")
    print("=" * 55)

    # 确保 Chaldea 源码可用（自动 clone 或 pull）
    print(f"\n📂 Chaldea 源码路径: {CHALDEA_ROOT}")
    _ensure_chaldea_source()

    if not GAMEDATA_DIR.exists():
        print(f"❌ 同步后仍未找到 Chaldea 数据目录: {GAMEDATA_DIR}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 参考文件输出到 docs/reference/（非 runtime 依赖，仅供开发参考）
    ref_dir = PROJECT_ROOT / "docs" / "reference"
    ref_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 1: FuncType 枚举 ---
    print("\n📋 Step 1: 解析 FuncType 枚举...")
    func_types = parse_dart_enum(FUNC_DART, "FuncType")
    _write_json(
        ref_dir / "func_types.json",
        {
            "enumName": "FuncType",
            "source": "func.dart",
            "count": len(func_types),
            "values": func_types,
        },
    )
    print(f"   ✅ 提取 {len(func_types)} 个 FuncType → docs/reference/")

    # --- Step 2: FuncTargetType 枚举 ---
    print("\n📋 Step 1b: 解析 FuncTargetType 枚举...")
    func_target_types = parse_dart_enum(FUNC_DART, "FuncTargetType")
    _write_json(
        ref_dir / "func_target_types.json",
        {
            "enumName": "FuncTargetType",
            "source": "func.dart",
            "count": len(func_target_types),
            "values": func_target_types,
        },
    )
    print(f"   ✅ 提取 {len(func_target_types)} 个 FuncTargetType → docs/reference/")

    # --- Step 3: BuffType 枚举 ---
    print("\n📋 Step 2: 解析 BuffType 枚举...")
    buff_types = parse_dart_enum(BUFF_DART, "BuffType")
    _write_json(
        ref_dir / "buff_types.json",
        {
            "enumName": "BuffType",
            "source": "buff.dart",
            "count": len(buff_types),
            "values": buff_types,
        },
    )
    print(f"   ✅ 提取 {len(buff_types)} 个 BuffType → docs/reference/")

    # --- Step 4: SkillEffect 分类 + validate 规则 + traits ---
    print("\n📋 Step 3: 解析 SkillEffect 效果分类...")
    effects = parse_effect_schema(EFFECT_DART)

    # 提取 validate 依赖的 Trait 常量值
    print("\n📋 Step 3b: 提取 validate 依赖的 Trait 常量...")
    validate_traits = extract_validate_traits(COMMON_DART)
    print(f"   ✅ 提取 {len(validate_traits)} 个 Trait 常量: {validate_traits}")

    # 提取 kBuffValueTriggerTypes 包含的 BuffType 列表
    print("\n📋 Step 3c: 提取 kBuffValueTriggerTypes...")
    trigger_buff_types = extract_trigger_buff_types(BUFF_DART)
    print(f"   ✅ 提取 {len(trigger_buff_types)} 个 trigger BuffType")

    # 将 validate 规则中的 trait 名解析为具体数值
    for effect in effects:
        if "validate" not in effect:
            continue
        rule = effect["validate"]
        if "trait" in rule:
            trait_name = rule["trait"]
            if trait_name in validate_traits:
                rule["traitValue"] = validate_traits[trait_name]
        elif "traits" in rule:
            rule["traitValues"] = [validate_traits[t] for t in rule["traits"] if t in validate_traits]

    validate_count = sum(1 for e in effects if "validate" in e)

    # 下载翻译数据并添加中文别名
    print("\n📋 Step 3d: 下载效果翻译数据...")
    enum_translations = download_enum_translations()
    et_count = len(enum_translations.get("effect_type", {}))
    bt_count = len(enum_translations.get("buff_type", {}))
    ft_count = len(enum_translations.get("func_type", {}))
    print(f"   ✅ 翻译数据: effect_type={et_count}, buff_type={bt_count}, func_type={ft_count}")

    _add_chinese_aliases(effects, enum_translations)
    aliased_count = sum(1 for e in effects if e.get("aliases_zh"))
    print(f"   ✅ {aliased_count}/{len(effects)} 个效果有中文别名")

    _write_json(
        OUTPUT_DIR / "effect_schema.json",
        {
            "source": "effect.dart",
            "count": len(effects),
            "categories": ["attack", "defence", "debuff", "others"],
            "traits": validate_traits,
            "triggerBuffTypes": trigger_buff_types,
            "effects": effects,
        },
    )
    print(f"   ✅ 提取 {len(effects)} 个效果分类 ({validate_count} 个含 validate 规则)")
    for cat in ["attack", "defence", "debuff", "others"]:
        cat_count = sum(1 for e in effects if e["category"] == cat)
        print(f"      {cat}: {cat_count} 个")

    # --- Step 5: SvtClass 枚举 ---
    print("\n📋 Step 4: 解析 SvtClass 枚举...")
    svt_classes = parse_dart_enum(COMMON_DART, "SvtClass")
    # 筛选出玩家可用的职阶（value 1-28, 排除 beast/lore/unknown 等）
    playable_classes = [c for c in svt_classes if c["value"] in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 23, 25, 28}]
    _write_json(
        OUTPUT_DIR / "class_mapping.json",
        {
            "enumName": "SvtClass",
            "source": "common.dart",
            "totalCount": len(svt_classes),
            "playableCount": len(playable_classes),
            "playable": playable_classes,
            "all": svt_classes,
        },
    )
    print(f"   ✅ 提取 {len(svt_classes)} 个职阶 ({len(playable_classes)} 可用)")

    # --- Step 6: 下载多语言映射 ---
    print("\n📋 Step 5: 下载多语言映射数据...")
    svt_names = download_mapping_data("svt_names.json")
    traits_map = download_mapping_data("trait.json")
    _write_json(
        OUTPUT_DIR / "mappings.json",
        {
            "svt_names": svt_names,
            "traits": traits_map,
        },
    )
    print(f"   ✅ 保存 mappings.json (含 {len(svt_names)} 个从者名, {len(traits_map)} 个特性)")

    # --- Step 7: 元数据 ---
    print("\n📋 Step 6: 生成元数据...")
    commit = get_chaldea_commit()
    meta = {
        "syncedAt": datetime.now(UTC).isoformat(),
        "chaldeaCommit": commit,
        "chaldeaPath": str(CHALDEA_ROOT),
        "files": {
            "func_types.json": len(func_types),
            "func_target_types.json": len(func_target_types),
            "buff_types.json": len(buff_types),
            "effect_schema.json": len(effects),
            "class_mapping.json": len(svt_classes),
        },
    }
    _write_json(OUTPUT_DIR / "_meta.json", meta)
    print(f"   ✅ Chaldea commit: {commit}")

    # 总结
    print("\n" + "=" * 55)
    print(f"✨ 同步完成! 输出目录: {OUTPUT_DIR}")
    print("   📁 共生成 6 个知识库文件")
    print("=" * 55)


def _write_json(path: Path, data: dict) -> None:
    """写入 JSON 文件。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
