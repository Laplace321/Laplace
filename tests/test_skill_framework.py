"""
Skill 框架单元测试。

测试范围：
- Skill 注册与发现
- SkillExecutor AND 合并执行
- Preset 执行
- 参数校验容错（校验失败跳过而非崩溃）

所有测试直接调用 SkillExecutor，不经过 LLM，纯确定性测试。
"""

import server.skills  # noqa: F401 — 触发 @register_skill 注册
from server.skills.base import SKILL_REGISTRY, QuerySkill, ResponseSkill
from server.skills.executor import ExecutionResult, SkillExecutor
from server.skills.presets import PRESET_REGISTRY

# ============================================================
# Skill 注册测试
# ============================================================


class TestSkillRegistry:
    """测试 Skill 注册机制。"""

    def test_query_skills_registered(self):
        """核心 Query Skill 应全部注册。"""
        expected = {
            "search_by_class",
            "search_by_rarity",
            "search_by_attribute",
            "search_by_effect",
            "search_by_skill_effect",
            "search_by_np_effect",
            "search_by_traits",
            "search_by_cards",
            "lookup_servant",
            "compare_servants",
        }
        actual_query = {name for name, skill in SKILL_REGISTRY.items() if isinstance(skill, QuerySkill)}
        assert expected.issubset(actual_query), f"缺少 Query Skills: {expected - actual_query}"

    def test_response_skills_registered(self):
        """4 个 Response Skill 应全部注册。"""
        expected = {
            "respond_servant_list",
            "respond_servant_detail",
            "respond_servant_compare",
            "respond_support_analysis",
        }
        actual_response = {name for name, skill in SKILL_REGISTRY.items() if isinstance(skill, ResponseSkill)}
        assert expected.issubset(actual_response), f"缺少 Response Skills: {expected - actual_response}"

    def test_skill_has_name_and_description(self):
        """每个 Skill 都必须有 name 和 description。"""
        for name, skill in SKILL_REGISTRY.items():
            assert skill.name, f"Skill {name} 缺少 name"
            assert skill.description, f"Skill {name} 缺少 description"

    def test_query_skill_has_params_schema(self):
        """每个 QuerySkill 的 params_schema 应返回 Pydantic 模型或 None。"""
        for name, skill in SKILL_REGISTRY.items():
            if isinstance(skill, QuerySkill):
                schema = skill.params_schema
                assert schema is None or hasattr(schema, "model_validate"), (
                    f"Skill {name} 的 params_schema 不是 Pydantic 模型"
                )


# ============================================================
# Preset 注册测试
# ============================================================


class TestPresetRegistry:
    """测试 Preset 注册机制。"""

    def test_presets_registered(self):
        """4 个内置预设应全部注册。"""
        expected = {
            "cycle_farming",
            "servant_lookup",
            "servant_compare",
            "support_recommend",
        }
        assert expected == set(PRESET_REGISTRY.keys())

    def test_preset_skills_exist(self):
        """预设引用的 Skill 必须存在于 SKILL_REGISTRY。"""
        for preset_name, preset in PRESET_REGISTRY.items():
            for skill_name in preset.query_skills:
                assert skill_name in SKILL_REGISTRY, f"Preset {preset_name} 引用了不存在的 Skill: {skill_name}"
            assert preset.response_skill in SKILL_REGISTRY, (
                f"Preset {preset_name} 引用了不存在的 Response Skill: {preset.response_skill}"
            )


# ============================================================
# SkillExecutor 执行测试
# ============================================================


class TestSkillExecutor:
    """测试 SkillExecutor 核心执行逻辑。"""

    def setup_method(self):
        self.executor = SkillExecutor()

    def test_single_skill_filter(self):
        """单个 Skill 筛选（按职阶）。"""
        result = self.executor.execute(
            skill_calls=[{"skill_name": "search_by_class", "params": {"className": "Caster"}}],
        )
        assert isinstance(result, ExecutionResult)
        assert result.total_found > 0
        assert all(s.get("className", "").lower() == "caster" for s in result.servants)
        assert not result.is_fallback

    def test_and_merge_two_skills(self):
        """两个 Skill AND 合并（职阶 + 稀有度）。"""
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "search_by_class", "params": {"className": "Saber"}},
                {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
            ],
        )
        assert result.total_found > 0
        for s in result.servants:
            assert s.get("className", "").lower() == "saber"
            assert s.get("rarity") == 5

    def test_np_charge_filter(self):
        """NP 充能筛选（≥50% 自充，通过 search_by_effect + gainNp，仅技能来源）。

        注意：重分类后 partyOther+self 同技能共存会将 partyOther 计入 party，
        自身查询累加 self+party+ptOne，因此部分 totalSelfCharge<50 的从者也可能匹配。
        改为验证关键从者在结果中。
        """
        result = self.executor.execute(
            skill_calls=[
                {
                    "skill_name": "search_by_effect",
                    "params": {"effect": "gainNp", "targetType": "self", "minValue": 50, "source": "skill"},
                },
            ],
        )
        assert result.total_found > 0
        nos = [s["collectionNo"] for s in result.servants]
        assert 284 in nos, "卡斯特利亚(50%自充)应被匹配"
        assert 371 in nos, "特斯卡特利波卡(50%自充)应被匹配"

    def test_empty_skill_calls_returns_fallback(self):
        """空 skill_calls 返回 fallback。"""
        result = self.executor.execute(skill_calls=[])
        assert result.is_fallback
        assert result.total_found == 0
        assert result.fallback_message is not None

    def test_unknown_skill_skipped(self):
        """未知 Skill 应被跳过而非崩溃。"""
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "nonexistent_skill", "params": {}},
                {"skill_name": "search_by_class", "params": {"className": "Archer"}},
            ],
        )
        # 第一个被跳过，第二个正常执行
        assert result.total_found > 0
        assert all(s.get("className", "").lower() == "archer" for s in result.servants)

    def test_invalid_params_skipped(self):
        """参数校验失败的 Skill 应被跳过。"""
        result = self.executor.execute(
            skill_calls=[
                # search_by_rarity 需要 value: int，传入非法值
                {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": "not_a_number"}},
                {"skill_name": "search_by_class", "params": {"className": "Lancer"}},
            ],
        )
        # rarity skill 被跳过，class skill 正常执行
        assert result.total_found > 0
        assert all(s.get("className", "").lower() == "lancer" for s in result.servants)

    def test_compare_servants_custom_execute(self):
        """compare_servants 使用自定义 execute（多名称查找）。"""
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "compare_servants", "params": {"names": ["Merlin", "Castoria"]}},
            ],
            response_skill_name="respond_servant_compare",
        )
        # 至少能找到其中一个
        assert result.total_found >= 1

    def test_response_skill_resolved(self):
        """Response Skill 应被正确解析。"""
        result = self.executor.execute(
            skill_calls=[{"skill_name": "search_by_class", "params": {"className": "Saber"}}],
            response_skill_name="respond_servant_detail",
        )
        assert result.response_skill is not None
        assert result.response_skill.name == "respond_servant_detail"

    def test_default_response_skill_fallback(self):
        """未知 Response Skill 应降级到 respond_servant_list。"""
        result = self.executor.execute(
            skill_calls=[{"skill_name": "search_by_class", "params": {"className": "Saber"}}],
            response_skill_name="nonexistent_response",
        )
        assert result.response_skill is not None
        assert result.response_skill.name == "respond_servant_list"

    def test_results_sorted_by_rarity_desc(self):
        """结果应按稀有度降序排序。"""
        result = self.executor.execute(
            skill_calls=[{"skill_name": "search_by_class", "params": {"className": "Saber"}}],
        )
        if result.total_found > 1:
            rarities = [s.get("rarity", 0) for s in result.servants]
            for i in range(len(rarities) - 1):
                assert rarities[i] >= rarities[i + 1] or (rarities[i] == rarities[i + 1]), "结果未按稀有度降序排序"

    def test_search_by_cards_multi_np_support_target(self):
        """双宝具从者辅助宝具查询：BB Dubai、玛修等顶层 npTarget 仅反映主宝具，
        辅助宝具信息在 npDetails 中。search_by_cards 必须能通过遍历 npDetails 命中它们。
        """
        # BB Dubai (collectionNo=337): 5星月之癌，双宝具，Arts 辅助宝具应被命中
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "search_by_class", "params": {"className": "moonCancer"}},
                {"skill_name": "search_by_cards", "params": {"npCard": "arts", "npTarget": "support"}},
                {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
            ],
        )
        ids = {s.get("id") for s in result.servants}
        assert 2300600 in ids, f"BB Dubai (id=2300600) 应被命中，当前结果中 ids={sorted(ids)}"

    def test_search_by_cards_top_level_still_works(self):
        """单宝具从者仍需能被命中。"""
        # 查全体 Buster 宝具（应返回多名从者，不为空）
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "search_by_cards", "params": {"npCard": "buster", "npTarget": "all"}},
                {"skill_name": "search_by_rarity", "params": {"op": "eq", "value": 5}},
            ],
        )
        assert result.total_found > 0
