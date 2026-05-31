"""
Laplace — Skill 注册表完整性测试

验证 SKILL_REGISTRY 中所有 Skill 的注册完整性：
1. 所有 query skill 文件均已注册
2. 所有 response skill 文件均已注册
3. 每个 Skill 的 name / description 非空
4. 每个 QuerySkill 的 params_schema（如有）能正常生成 JSON Schema
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.skills.base import SKILL_REGISTRY, QuerySkill, ResponseSkill

# ── 注册表基本完整性 ──


def test_registry_not_empty():
    """注册表不应为空。"""
    assert len(SKILL_REGISTRY) > 0, "SKILL_REGISTRY 为空，Skill 模块未正确导入"


def test_all_skills_have_name():
    """每个已注册 Skill 的 name 必须非空。"""
    for key, skill in SKILL_REGISTRY.items():
        assert skill.name, f"Skill（key='{key}'）的 name 为空"


def test_all_skills_have_description():
    """每个已注册 Skill 的 description 必须非空。"""
    for key, skill in SKILL_REGISTRY.items():
        assert skill.description, f"Skill '{key}' 缺少 description"


# ── params_schema 合法性 ──


def test_query_skill_params_schema_valid():
    """每个有 params_schema 的 QuerySkill，其 Pydantic 模型能正常生成 JSON Schema。"""
    for skill_name, skill in SKILL_REGISTRY.items():
        if not isinstance(skill, QuerySkill):
            continue
        schema_cls = skill.params_schema
        if schema_cls is None:
            continue
        try:
            json_schema = schema_cls.model_json_schema()
            assert isinstance(json_schema, dict), f"Skill '{skill_name}' 的 JSON Schema 不是 dict"
            assert "properties" in json_schema or "type" in json_schema, (
                f"Skill '{skill_name}' 的 JSON Schema 缺少 properties 或 type 字段"
            )
        except Exception as err:
            pytest.fail(f"Skill '{skill_name}' 的 params_schema 生成 JSON Schema 失败: {err}")


# ── 文件 vs 注册表一致性 ──


def test_all_query_skill_files_registered():
    """server/skills/query/ 下所有 Skill 文件都应在 SKILL_REGISTRY 中有对应注册。"""
    query_dir = Path(__file__).parent.parent / "server" / "skills" / "query"
    assert query_dir.exists(), f"Query skill 目录不存在: {query_dir}"

    file_modules = {f.stem for f in query_dir.glob("*.py") if f.stem != "__init__" and not f.stem.startswith("_")}

    registered_modules = set()
    for skill in SKILL_REGISTRY.values():
        if isinstance(skill, QuerySkill):
            module_name = type(skill).__module__
            registered_modules.add(module_name.rsplit(".", 1)[-1])

    unregistered = file_modules - registered_modules
    assert not unregistered, (
        f"以下 Query Skill 文件存在但未注册: {sorted(unregistered)}。"
        f"请检查 server/skills/__init__.py 的 _SKILL_MODULES 列表"
    )


def test_all_response_skill_files_registered():
    """server/skills/response/ 下所有 Skill 文件都应在 SKILL_REGISTRY 中有对应注册。"""
    response_dir = Path(__file__).parent.parent / "server" / "skills" / "response"
    assert response_dir.exists(), f"Response skill 目录不存在: {response_dir}"

    file_modules = {f.stem for f in response_dir.glob("*.py") if f.stem != "__init__" and not f.stem.startswith("_")}

    registered_modules = set()
    for skill in SKILL_REGISTRY.values():
        if isinstance(skill, ResponseSkill):
            module_name = type(skill).__module__
            registered_modules.add(module_name.rsplit(".", 1)[-1])

    unregistered = file_modules - registered_modules
    assert not unregistered, (
        f"以下 Response Skill 文件存在但未注册: {sorted(unregistered)}。"
        f"请检查 server/skills/__init__.py 的 _SKILL_MODULES 列表"
    )


# ── 注册表数量基线 ──


def test_minimum_query_skill_count():
    """QuerySkill 数量不应低于已知基线（防止意外批量丢失注册）。"""
    query_count = sum(1 for s in SKILL_REGISTRY.values() if isinstance(s, QuerySkill))
    # 当前已有 20 个 query skill 文件，设基线为 15（留余量给合法删除）
    assert query_count >= 15, f"QuerySkill 数量 {query_count} 低于基线 15，可能存在注册丢失"


def test_minimum_response_skill_count():
    """ResponseSkill 数量不应低于已知基线。"""
    response_count = sum(1 for s in SKILL_REGISTRY.values() if isinstance(s, ResponseSkill))
    # 当前已有 6 个 response skill 文件，设基线为 4
    assert response_count >= 4, f"ResponseSkill 数量 {response_count} 低于基线 4，可能存在注册丢失"
