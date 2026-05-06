import json
from pathlib import Path

from server.sync_chaldea import parse_dart_enum, parse_effect_schema

# 已生成的知识库路径
KNOWLEDGE_DIR = Path(__file__).parent.parent / "server" / "knowledge"


def test_parse_dart_enum_extracts_values_labels_and_extra(tmp_path):
    dart_file = tmp_path / "common.dart"
    dart_file.write_text(
        """
enum SvtClass {
  saber(1, '剣', 1),
  archer(2, '弓', 2),
  unknown(-1);

  final int value;
}
""",
        encoding="utf-8",
    )

    assert parse_dart_enum(dart_file, "SvtClass") == [
        {"name": "saber", "value": 1, "label": "剣", "baseClassId": 1},
        {"name": "archer", "value": 2, "label": "弓", "baseClassId": 2},
        {"name": "unknown", "value": -1},
    ]


def test_parse_effect_schema_extracts_buff_func_and_constructor_forms(tmp_path):
    dart_file = tmp_path / "effect.dart"
    dart_file.write_text(
        """
class SkillEffect {
  static List<SkillEffect> kAttack = [upAtk, gainNp, upCommand];
  static List<SkillEffect> kDefence = [invincible];
  static List<SkillEffect> kDebuffRelated = [];
  static List<SkillEffect> kOthers = [];

  static SkillEffect upAtk = SkillEffect._buff('upAtk', BuffType.upAtk);
  static SkillEffect gainNp = SkillEffect._func('gainNp', FuncType.gainNp);
  static SkillEffect upCommand = SkillEffect(
    'upArts',
    funcTypes: [FuncType.addState],
    buffTypes: [BuffType.upCommandall, BuffType.upCommandnp],
  );
  static SkillEffect invincible = SkillEffect._buff('invincible', BuffType.invincible);
}
""",
        encoding="utf-8",
    )

    effects = {effect["name"]: effect for effect in parse_effect_schema(dart_file)}

    assert effects["upAtk"]["category"] == "attack"
    assert effects["upAtk"]["buffTypes"] == ["upAtk"]
    assert effects["gainNp"]["funcTypes"] == ["gainNp"]
    assert effects["upArts"]["funcTypes"] == ["addState"]
    assert effects["upArts"]["buffTypes"] == ["upCommandall", "upCommandnp"]
    assert effects["invincible"]["category"] == "defence"
    assert effects["invincible"]["aliases_zh"] == ["无敌"]


# ============================================================
# 知识库数据量守护测试（防止 Chaldea 源码格式变更导致静默退化）
# ============================================================

EFFECT_SCHEMA_PATH = KNOWLEDGE_DIR / "effect_schema.json"
FUNC_TYPES_PATH = KNOWLEDGE_DIR / "func_types.json"
BUFF_TYPES_PATH = KNOWLEDGE_DIR / "buff_types.json"

# 下限阈值：基于 2026-05-06 实际提取数量设定，允许未来增长但不允许大幅减少
MIN_SKILL_EFFECTS = 50  # 当前 55
MIN_FUNC_TYPES = 35  # 当前 40（去重后的 funcType 值）
MIN_BUFF_TYPES = 40  # 当前 44（去重后的 buffType 值）


def test_effect_schema_has_minimum_effects():
    """验证 effect_schema.json 包含足够数量的 SkillEffect 条目。"""
    schema = json.loads(EFFECT_SCHEMA_PATH.read_text(encoding="utf-8"))
    effects = schema.get("effects", [])
    assert len(effects) >= MIN_SKILL_EFFECTS, (
        f"SkillEffect 数量 {len(effects)} 低于预期下限 {MIN_SKILL_EFFECTS}，"
        f"可能是 Chaldea effect.dart 格式变更导致解析失效"
    )


def test_effect_schema_covers_minimum_func_types():
    """验证 effect_schema.json 中引用的 FuncType 数量达标。"""
    schema = json.loads(EFFECT_SCHEMA_PATH.read_text(encoding="utf-8"))
    func_types = set()
    for e in schema.get("effects", []):
        for ft in e.get("funcTypes", []):
            func_types.add(ft)
    assert len(func_types) >= MIN_FUNC_TYPES, f"FuncType 数量 {len(func_types)} 低于预期下限 {MIN_FUNC_TYPES}"


def test_effect_schema_covers_minimum_buff_types():
    """验证 effect_schema.json 中引用的 BuffType 数量达标。"""
    schema = json.loads(EFFECT_SCHEMA_PATH.read_text(encoding="utf-8"))
    buff_types = set()
    for e in schema.get("effects", []):
        for bt in e.get("buffTypes", []):
            buff_types.add(bt)
    assert len(buff_types) >= MIN_BUFF_TYPES, f"BuffType 数量 {len(buff_types)} 低于预期下限 {MIN_BUFF_TYPES}"


def test_effect_schema_every_entry_has_required_fields():
    """验证每个 SkillEffect 条目都包含必要字段。"""
    schema = json.loads(EFFECT_SCHEMA_PATH.read_text(encoding="utf-8"))
    for effect in schema.get("effects", []):
        assert "name" in effect, f"缺少 name 字段: {effect}"
        assert "category" in effect, f"{effect['name']} 缺少 category"
        # 大部分效果至少有 funcTypes 或 buffTypes 之一
        # 少数特殊效果（如 triggerFunc）可能两者都为空，属于合法情况
        has_func = bool(effect.get("funcTypes"))
        has_buff = bool(effect.get("buffTypes"))
        has_aliases = bool(effect.get("aliases_zh"))
        assert has_func or has_buff or has_aliases, f"{effect['name']} 既没有 funcTypes/buffTypes 也没有 aliases_zh"


def test_effect_schema_has_key_effects():
    """验证关键效果（高频查询）必须存在于 schema 中。"""
    schema = json.loads(EFFECT_SCHEMA_PATH.read_text(encoding="utf-8"))
    effect_names = {e["name"] for e in schema.get("effects", [])}

    # 这些是用户最常查询的效果，绝对不能丢失
    critical_effects = [
        "gainNp",  # NP 充能
        "upAtk",  # 攻击力提升
        "invincible",  # 无敌
        "guts",  # 毅力
        "avoidance",  # 回避
        "upCriticaldamage",  # 暴击威力
        "upArts",  # 蓝卡提升
        "upQuick",  # 绿卡提升
        "upBuster",  # 红卡提升
        "upNpdamage",  # 宝具威力
    ]
    for name in critical_effects:
        assert name in effect_names, f"关键效果 '{name}' 在 effect_schema.json 中缺失"
