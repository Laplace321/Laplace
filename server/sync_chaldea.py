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
# 2. SkillEffect 分类解析器
# ============================================================


def parse_effect_schema(file_path: Path) -> list[dict]:
    """
    从 effect.dart 提取 SkillEffect 静态字段定义。

    解析模式：
        static SkillEffect upAtk = SkillEffect('upAtk', buffTypes: [BuffType.upAtk]);
        static SkillEffect gainNp = SkillEffect('gainNp', funcTypes: [FuncType.gainNp, ...]);
        static SkillEffect avoidance = SkillEffect('avoidance', buffTypes: [BuffType.avoidance, BuffType.avoidanceIndividuality]);
        static SkillEffect._buff('name', BuffType.xxx)
        static SkillEffect._func('name', FuncType.xxx)
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

    # 模式 1: SkillEffect._buff('name', BuffType.xxx)
    for m in re.finditer(
        r"static\s+SkillEffect\s+(\w+)\s*=\s*SkillEffect\._buff\(\s*"
        r"['\"](\w+)['\"],\s*BuffType\.(\w+)",
        content,
    ):
        effects.append(
            {
                "name": m.group(2),
                "category": categories.get(m.group(1), "unknown"),
                "funcTypes": [],
                "buffTypes": [m.group(3)],
            }
        )

    # 模式 2: SkillEffect._func('name', FuncType.xxx)
    for m in re.finditer(
        r"static\s+SkillEffect\s+(\w+)\s*=\s*SkillEffect\._func\(\s*"
        r"['\"](\w+)['\"],\s*FuncType\.(\w+)",
        content,
    ):
        effects.append(
            {
                "name": m.group(2),
                "category": categories.get(m.group(1), "unknown"),
                "funcTypes": [m.group(3)],
                "buffTypes": [],
            }
        )

    # 模式 3: SkillEffect('name', funcTypes: [...], buffTypes: [...])
    # 需要更灵活的正则——找到完整的构造函数调用
    for m in re.finditer(
        r"static\s+SkillEffect\s+(\w+)\s*=\s*SkillEffect\(\s*\n?\s*"
        r"['\"](\w+)['\"]",
        content,
    ):
        var_name = m.group(1)
        effect_name = m.group(2)

        # 从匹配点开始，找到闭合的 );
        start = m.start()
        # 向后搜索到 );
        depth = 0
        end = start
        for i in range(start, len(content)):
            if content[i] == "(":
                depth += 1
            elif content[i] == ")":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        block = content[start:end]

        # 已被 _buff/_func 模式处理的跳过
        if "._buff(" in block or "._func(" in block:
            continue

        # 提取 funcTypes
        func_types = []
        ft_match = re.search(r"funcTypes:\s*\[(.*?)\]", block, re.DOTALL)
        if ft_match:
            func_types = re.findall(r"FuncType\.(\w+)", ft_match.group(1))

        # 提取 buffTypes
        buff_types = []
        bt_match = re.search(r"buffTypes:\s*\[(.*?)\]", block, re.DOTALL)
        if bt_match:
            buff_types = re.findall(r"BuffType\.(\w+)", bt_match.group(1))

        # 去重检查
        if any(e["name"] == effect_name for e in effects):
            continue

        effects.append(
            {
                "name": effect_name,
                "category": categories.get(var_name, "unknown"),
                "funcTypes": func_types,
                "buffTypes": buff_types,
            }
        )

    # 添加中文别名
    _add_chinese_aliases(effects)

    return effects


# 效果名 → 中文别名映射（手动维护）
EFFECT_ALIASES_ZH = {
    "upAtk": ["攻击力提升", "加攻"],
    "upQuick": ["Quick性能提升", "绿卡提升"],
    "upArts": ["Arts性能提升", "蓝卡提升"],
    "upBuster": ["Buster性能提升", "红卡提升"],
    "upDamage": ["特攻", "特攻伤害"],
    "addDamage": ["附加伤害"],
    "upCriticaldamage": ["暴击威力提升", "暴击伤害", "爆伤"],
    "upCriticalpoint": ["获得暴击星", "产星", "出星"],
    "upStarweight": ["暴击星集中度提升", "集星"],
    "gainStar": ["获得暴击星", "给星", "产星"],
    "regainStar": ["每回合获得暴击星", "出星状态"],
    "damageNpSP": ["特攻宝具"],
    "upNpdamage": ["宝具威力提升", "宝威", "宝伤"],
    "gainNp": ["NP增加", "自充", "充能", "群充", "NP获取"],
    "regainNp": ["每回合NP增加", "NP回复", "缓充"],
    "upDropnp": ["NP获取量提升", "黄金律"],
    "upChagetd": ["充能阶段提升", "OC提升"],
    "breakAvoidance": ["必中"],
    "pierceInvincible": ["无敌贯通"],
    "pierceDefence": ["无视防御"],
    "upDefence": ["防御力提升", "加防"],
    "subSelfdamage": ["被伤害减免", "减伤"],
    "avoidance": ["回避"],
    "invincible": ["无敌"],
    "guts": ["毅力", "战续", "根性"],
    "upHate": ["嘲讽", "集火"],
    "downCriticalRateDamageTaken": ["被暴击率降低"],
    "gainHp": ["HP回复", "回血"],
    "upGainHp": ["回复量提升"],
    "regainHp": ["HP再生", "每回合回复"],
    "addMaxhp": ["最大HP提升"],
    "reduceHp": ["HP减少"],
    "upTolerance": ["弱体耐性提升", "异常耐性"],
    "avoidStateNegative": ["弱体无效", "异常无效"],
    "upGrantstate": ["状态付与率提升"],
    "upGrantstatePositive": ["强化成功率提升"],
    "upGrantstateNegative": ["弱体成功率提升"],
    "upReceivePositiveEffect": ["被强化成功率提升"],
    "subState": ["状态解除", "强化解除"],
    "subStatePositive": ["正面状态解除"],
    "subStateNegative": ["负面状态解除", "弱体解除"],
    "upToleranceSubstate": ["强化解除耐性"],
    "instantDeath": ["即死"],
    "upResistInstantdeath": ["即死耐性提升"],
    "upGrantInstantdeath": ["即死成功率提升"],
    "avoidInstantdeath": ["即死无效"],
    "shortenSkill": ["技能CD缩减", "减CD"],
    "fieldIndividuality": ["场地特性变更"],
    "servantFriendshipUp": ["羁绊获取量提升"],
    "qpUp": ["QP获取量提升"],
    "expUp": ["经验值获取量提升"],
    "userEquipExpUp": ["魔术礼装经验值提升"],
    "friendPointUp": ["友情点获取量提升"],
    "eventDropUp": ["活动掉落提升"],
    "triggerFunc": ["触发型技能", "条件发动"],
}


def _add_chinese_aliases(effects: list[dict]) -> None:
    """为效果列表添加中文别名。"""
    for effect in effects:
        effect["aliases_zh"] = EFFECT_ALIASES_ZH.get(effect["name"], [])


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

    # --- Step 1: FuncType 枚举 ---
    print("\n📋 Step 1: 解析 FuncType 枚举...")
    func_types = parse_dart_enum(FUNC_DART, "FuncType")
    _write_json(
        OUTPUT_DIR / "func_types.json",
        {
            "enumName": "FuncType",
            "source": "func.dart",
            "count": len(func_types),
            "values": func_types,
        },
    )
    print(f"   ✅ 提取 {len(func_types)} 个 FuncType")

    # --- Step 2: FuncTargetType 枚举 ---
    print("\n📋 Step 1b: 解析 FuncTargetType 枚举...")
    func_target_types = parse_dart_enum(FUNC_DART, "FuncTargetType")
    _write_json(
        OUTPUT_DIR / "func_target_types.json",
        {
            "enumName": "FuncTargetType",
            "source": "func.dart",
            "count": len(func_target_types),
            "values": func_target_types,
        },
    )
    print(f"   ✅ 提取 {len(func_target_types)} 个 FuncTargetType")

    # --- Step 3: BuffType 枚举 ---
    print("\n📋 Step 2: 解析 BuffType 枚举...")
    buff_types = parse_dart_enum(BUFF_DART, "BuffType")
    _write_json(
        OUTPUT_DIR / "buff_types.json",
        {
            "enumName": "BuffType",
            "source": "buff.dart",
            "count": len(buff_types),
            "values": buff_types,
        },
    )
    print(f"   ✅ 提取 {len(buff_types)} 个 BuffType")

    # --- Step 4: SkillEffect 分类 ---
    print("\n📋 Step 3: 解析 SkillEffect 效果分类...")
    effects = parse_effect_schema(EFFECT_DART)
    _write_json(
        OUTPUT_DIR / "effect_schema.json",
        {
            "source": "effect.dart",
            "count": len(effects),
            "categories": ["attack", "defence", "debuff", "others"],
            "effects": effects,
        },
    )
    print(f"   ✅ 提取 {len(effects)} 个效果分类")
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
