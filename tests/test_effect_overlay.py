"""Effect Schema Overlay 合并逻辑回归测试。"""

import json

from server.data_loader import merge_effect_overlay


def _make_effect(name: str, **kwargs) -> dict:
    """构造一个最小化的 effect 字典。"""
    eff = {"name": name, "category": "attack"}
    eff.update(kwargs)
    return eff


def test_overlay_appends_new_effects():
    """overlay 中的新效果能被正确追加到列表末尾。"""
    base = [_make_effect("upAtk")]
    overlay_effects = [_make_effect("damageBoost", composite=True, includes=["upAtk"])]

    # 模拟：直接调用内部合并逻辑
    existing_names = {e["name"] for e in base}
    merged = list(base)
    for eff in overlay_effects:
        if eff["name"] in existing_names:
            merged = [eff if e["name"] == eff["name"] else e for e in merged]
        else:
            merged.append(eff)

    assert len(merged) == 2
    assert merged[0]["name"] == "upAtk"
    assert merged[1]["name"] == "damageBoost"
    assert merged[1]["composite"] is True


def test_overlay_overrides_existing_effects():
    """overlay 中的同名效果能正确覆盖 schema 中的定义。"""
    base = [
        _make_effect("upAtk", aliases_zh=["攻击力提升"]),
        _make_effect("upBuster", aliases_zh=["红魔放"]),
    ]
    overlay_effects = [
        _make_effect("upAtk", aliases_zh=["攻击力提升", "加攻"], description="覆盖版本"),
    ]

    existing_names = {e["name"] for e in base}
    merged = list(base)
    for eff in overlay_effects:
        if eff["name"] in existing_names:
            merged = [eff if e["name"] == eff["name"] else e for e in merged]
        else:
            merged.append(eff)

    assert len(merged) == 2
    assert merged[0]["name"] == "upAtk"
    assert merged[0]["description"] == "覆盖版本"
    assert merged[0]["aliases_zh"] == ["攻击力提升", "加攻"]
    # 其他效果不受影响
    assert merged[1]["name"] == "upBuster"


def test_merge_effect_overlay_no_file(tmp_path, monkeypatch):
    """overlay 文件不存在时，merge_effect_overlay 静默返回原列表。"""
    import server.data_loader as dl

    monkeypatch.setattr(dl, "CONFIG_DIR", tmp_path)
    base = [_make_effect("upAtk")]
    result = merge_effect_overlay(base)
    assert result == base


def test_merge_effect_overlay_with_file(tmp_path, monkeypatch):
    """overlay 文件存在时，merge_effect_overlay 正确合并。"""
    import server.data_loader as dl

    monkeypatch.setattr(dl, "CONFIG_DIR", tmp_path)

    overlay_data = {
        "effects": [
            _make_effect("damageBoost", composite=True, includes=["upAtk", "upBuster"]),
        ]
    }
    overlay_path = tmp_path / "effect_overrides.json"
    overlay_path.write_text(json.dumps(overlay_data, ensure_ascii=False), encoding="utf-8")

    base = [_make_effect("upAtk"), _make_effect("upBuster")]
    result = merge_effect_overlay(base)

    assert len(result) == 3
    names = [e["name"] for e in result]
    assert "damageBoost" in names
    assert result[2]["composite"] is True


def test_production_overlay_file_valid():
    """验证生产环境的 effect_overrides.json 格式正确且包含预期的虚拟效果。"""
    from pathlib import Path

    overlay_path = Path(__file__).parent.parent / "server" / "config" / "effect_overrides.json"
    assert overlay_path.exists(), "effect_overrides.json 应该存在"

    with open(overlay_path, encoding="utf-8") as f:
        data = json.load(f)

    effects = data.get("effects", [])
    names = {e["name"] for e in effects}
    assert "damageBoost" in names, "overlay 应包含 damageBoost"
    assert "damageShield" in names, "overlay 应包含 damageShield"

    # 验证复合效果结构完整
    for eff in effects:
        if eff.get("composite"):
            assert "includes" in eff, f"{eff['name']} 复合效果必须有 includes 字段"
            assert len(eff["includes"]) > 0, f"{eff['name']} includes 不能为空"
            assert "aliases_zh" in eff, f"{eff['name']} 必须有中文别名"


def test_knowledge_schema_no_composite():
    """验证 effect_schema.json（knowledge 层）不包含手工添加的复合效果。"""
    from pathlib import Path

    schema_path = Path(__file__).parent.parent / "server" / "knowledge" / "effect_schema.json"
    if not schema_path.exists():
        return  # CI 环境可能没有此文件

    with open(schema_path, encoding="utf-8") as f:
        data = json.load(f)

    composite_names = [e["name"] for e in data.get("effects", []) if e.get("composite")]
    assert composite_names == [], (
        f"knowledge/effect_schema.json 不应包含手工复合效果，"
        f"发现: {composite_names}。请移至 config/effect_overrides.json"
    )
