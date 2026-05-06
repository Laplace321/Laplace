"""翻译映射一致性校验测试。

验证 config/translations.json 的翻译 key 覆盖了 knowledge/class_mapping.json 中
所有可玩职阶，防止预消化翻译与知识库脱节。
"""

import json
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "server" / "config"
KNOWLEDGE_DIR = Path(__file__).parent.parent / "server" / "knowledge"


def _load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_class_translation_covers_all_playable_classes():
    """translations.json 的 className 必须覆盖所有可玩职阶。"""
    translations = _load_json(CONFIG_DIR / "translations.json")
    class_mapping = _load_json(KNOWLEDGE_DIR / "class_mapping.json")

    playable_names = {entry["name"].lower() for entry in class_mapping.get("playable", [])}
    translated_names = {k.lower() for k in translations["className"].keys()}

    missing = playable_names - translated_names
    assert not missing, (
        f"以下可玩职阶缺少中文翻译: {sorted(missing)}，请在 server/config/translations.json 的 className 中补充"
    )


def test_np_card_translation_covers_standard_types():
    """translations.json 的 npCard 必须覆盖三色卡。"""
    translations = _load_json(CONFIG_DIR / "translations.json")
    np_card_keys = {k.lower() for k in translations["npCard"].keys()}

    required = {"buster", "arts", "quick"}
    missing = required - np_card_keys
    assert not missing, f"npCard 翻译缺少: {sorted(missing)}"


def test_np_target_translation_covers_standard_types():
    """translations.json 的 npTarget 必须覆盖全体/单体/辅助。"""
    translations = _load_json(CONFIG_DIR / "translations.json")
    np_target_keys = {k.lower() for k in translations["npTarget"].keys()}

    required = {"all", "one", "support"}
    missing = required - np_target_keys
    assert not missing, f"npTarget 翻译缺少: {sorted(missing)}"


def test_translations_json_has_all_required_sections():
    """translations.json 必须包含 className/npCard/npTarget 三个顶层 key。"""
    translations = _load_json(CONFIG_DIR / "translations.json")
    for key in ("className", "npCard", "npTarget"):
        assert key in translations, f"translations.json 缺少顶层 key: {key}"
        assert isinstance(translations[key], dict), f"translations.json[{key}] 应为 dict"
        assert len(translations[key]) > 0, f"translations.json[{key}] 不能为空"
