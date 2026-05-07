"""
Skill-Based Architecture — SkillExecutor 集成测试

测试 SkillExecutor 的 AND 合并逻辑、preset 模式 Skills 实例化、降级兜底逻辑。
"""

from unittest.mock import patch

from server.skills.executor import ExecutionResult, SkillExecutor
from server.skills.presets import PRESET_REGISTRY

# 复用 test_query_executor.py 的共享测试数据
from tests.test_query_executor import NICKNAMES, SERVANTS

_executor = SkillExecutor()


def names(result: ExecutionResult) -> list[str]:
    return [s["name"] for s in result.servants]


# === AND 合并测试 ===


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_and_merge_two_skills(mock_db):
    """两个 QuerySkill AND 合并：五星 + 剑阶 → 只有 Altria Pendragon。"""
    skill_calls = [
        {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
        {"skill_name": "search_by_class", "params": {"class_name": "saber"}},
    ]
    result = _executor.execute(skill_calls)
    assert not result.is_fallback
    assert names(result) == ["Altria Pendragon"]


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_and_merge_three_skills(mock_db):
    """三个 QuerySkill AND 合并：五星 + 充能>=30 + arts 宝具。"""
    skill_calls = [
        {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
        {"skill_name": "search_by_np_charge", "params": {"op": "gte", "value": 30}},
        {"skill_name": "search_by_cards", "params": {"np_card": "arts"}},
    ]
    result = _executor.execute(skill_calls)
    assert not result.is_fallback
    # 五星 + 充能>=30 + arts宝具: Moriarty(50,arts) + Caster(50,arts)
    assert names(result) == ["James Moriarty", "Altria Caster"]


# === Preset 模式测试 ===


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_preset_cycle_farming(mock_db):
    """preset cycle_farming: 默认参数 npCharge>=30。"""
    preset = PRESET_REGISTRY["cycle_farming"]
    skill_calls = []
    for skill_name in preset.query_skills:
        params = preset.param_template.get(skill_name, {})
        if params:
            skill_calls.append({"skill_name": skill_name, "params": params})

    result = _executor.execute(skill_calls, preset.response_skill)
    assert not result.is_fallback
    # 充能>=30: Altria(30), Moriarty(50), Caster(50) → 按稀有度排
    assert result.total_found == 3
    assert "Altria Pendragon" in names(result)


@patch("server.skills.executor.load_database", return_value=SERVANTS)
@patch("server.skills.query.lookup_servant.load_nicknames", return_value=NICKNAMES)
def test_preset_servant_lookup(mock_nick, mock_db):
    """preset servant_lookup: 按名称查询。"""
    preset = PRESET_REGISTRY["servant_lookup"]
    skill_calls = [
        {"skill_name": "lookup_servant", "params": {"name": "呆毛"}},
    ]
    result = _executor.execute(skill_calls, preset.response_skill)
    assert not result.is_fallback
    assert names(result) == ["Altria Pendragon"]


def test_preset_registry_completeness():
    """验证 4 个初始预设都已注册。"""
    expected = {"cycle_farming", "servant_compare", "support_recommend", "servant_lookup"}
    assert set(PRESET_REGISTRY.keys()) == expected


# === 降级兜底测试 ===


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_fallback_unknown_skill(mock_db):
    """未知 Skill 名 → 降级。"""
    result = _executor.execute(
        [{"skill_name": "nonexistent_skill", "params": {}}],
    )
    assert result.is_fallback
    assert "未知" in result.fallback_message


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_fallback_empty_skill_calls(mock_db):
    """空 SkillCall 列表 → 降级。"""
    result = _executor.execute([])
    assert result.is_fallback
    assert "没有有效" in result.fallback_message


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_fallback_empty_results(mock_db):
    """合法 Skill 但查询结果为空 → 降级。"""
    result = _executor.execute(
        [{"skill_name": "search_by_class", "params": {"class_name": "avenger"}}],
    )
    assert result.is_fallback
    assert "未找到" in result.fallback_message


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_response_skill_resolution(mock_db):
    """指定 Response Skill 正确解析。"""
    result = _executor.execute(
        [{"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}}],
        response_skill_name="respond_servant_compare",
    )
    assert not result.is_fallback
    assert result.response_skill is not None
    assert result.response_skill.name == "respond_servant_compare"


@patch("server.skills.executor.load_database", return_value=SERVANTS)
def test_response_skill_fallback_to_default(mock_db):
    """未知 Response Skill → 降级到 respond_servant_list。"""
    result = _executor.execute(
        [{"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}}],
        response_skill_name="nonexistent_response",
    )
    assert not result.is_fallback
    assert result.response_skill is not None
    assert result.response_skill.name == "respond_servant_list"
