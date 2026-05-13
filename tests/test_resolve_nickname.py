"""
resolve_nickname Skill 单元测试。

测试范围：
- LRU 缓存命中/未命中
- DB 存在性校验成功/失败
- SkillExecutor fallback 链触发条件
- Mock LLM 调用（不依赖真实网络）

所有测试直接调用 Skill / SkillExecutor，纯确定性测试。
"""

from unittest.mock import patch

import server.skills  # noqa: F401 — 触发 @register_skill 注册
from server.skills.base import SKILL_REGISTRY
from server.skills.executor import SkillExecutor
from server.skills.query.resolve_nickname import (
    ResolveNickname,
    _cache_get,
    _cache_set,
    _nickname_cache,
    _validate_in_db,
)


class TestLRUCache:
    """测试 LRU 缓存机制。"""

    def setup_method(self):
        """每个测试前清空缓存。"""
        _nickname_cache.clear()

    def test_cache_miss(self):
        """缓存未命中时返回 (False, None)。"""
        hit, value = _cache_get("some_key")
        assert hit is False
        assert value is None

    def test_cache_hit_after_set(self):
        """写入后应命中。"""
        _cache_set("test_key", "阿尔托莉雅·潘德拉贡")
        hit, value = _cache_get("test_key")
        assert hit is True
        assert value == "阿尔托莉雅·潘德拉贡"

    def test_cache_does_not_store_none(self):
        """None 值不应被缓存。"""
        _cache_set("fail_key", None)
        hit, value = _cache_get("fail_key")
        assert hit is False
        assert value is None

    def test_cache_lru_eviction(self):
        """超过容量时应淘汰最旧的条目。"""
        from server.skills.query.resolve_nickname import _CACHE_MAX_SIZE

        # 写入超过容量的条目
        for i in range(_CACHE_MAX_SIZE + 10):
            _cache_set(f"key_{i}", f"value_{i}")

        # 最早的 10 个应该被淘汰
        for i in range(10):
            hit, _ = _cache_get(f"key_{i}")
            assert hit is False

        # 较新的应该还在
        hit, value = _cache_get(f"key_{_CACHE_MAX_SIZE + 5}")
        assert hit is True
        assert value == f"value_{_CACHE_MAX_SIZE + 5}"


class TestDBValidation:
    """测试 DB 存在性校验。"""

    def test_valid_name_returns_matches(self):
        """存在的从者名应返回匹配结果。"""
        # "梅林" 是确实存在的从者
        matches = _validate_in_db("梅林")
        assert len(matches) >= 1
        # 验证确实是梅林
        names = [s.get("aliasCN", "") for s in matches]
        assert any("梅林" in n for n in names)

    def test_invalid_name_returns_empty(self):
        """不存在的从者名应返回空列表。"""
        matches = _validate_in_db("这个名字绝对不存在XYZ123")
        assert matches == []

    def test_unknown_returns_empty(self):
        """'unknown' 应返回空列表。"""
        matches = _validate_in_db("unknown")
        assert matches == []

    def test_empty_string_returns_empty(self):
        """空字符串应返回空列表。"""
        matches = _validate_in_db("")
        assert matches == []


class TestResolveNicknameSkill:
    """测试 ResolveNickname Skill 执行逻辑。"""

    def setup_method(self):
        """每个测试前清空缓存。"""
        _nickname_cache.clear()

    def test_skill_registered(self):
        """resolve_nickname 应注册在 SKILL_REGISTRY 中。"""
        assert "resolve_nickname" in SKILL_REGISTRY
        skill = SKILL_REGISTRY["resolve_nickname"]
        assert isinstance(skill, ResolveNickname)
        assert skill.name == "resolve_nickname"
        assert skill.domain == "servant"

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_llm_success_with_valid_name(self, mock_llm):
        """LLM 返回有效从者名时应匹配成功。"""
        mock_llm.return_value = "梅林"

        from server.query_executor import load_database

        db = load_database()
        skill = SKILL_REGISTRY["resolve_nickname"]
        results = skill.execute(db, {"name": "花之魔术师"})

        assert len(results) >= 1
        mock_llm.assert_called_once_with("花之魔术师")

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_llm_returns_unknown(self, mock_llm):
        """LLM 返回 unknown 时应返回空结果。"""
        mock_llm.return_value = "unknown"

        from server.query_executor import load_database

        db = load_database()
        skill = SKILL_REGISTRY["resolve_nickname"]
        results = skill.execute(db, {"name": "完全胡扯的名字"})

        assert results == []

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_llm_returns_nonexistent_name(self, mock_llm):
        """LLM 返回不存在的名称时（幻觉），DB 校验应拦截。"""
        mock_llm.return_value = "根本不存在的从者ABC"

        from server.query_executor import load_database

        db = load_database()
        skill = SKILL_REGISTRY["resolve_nickname"]
        results = skill.execute(db, {"name": "某个昵称"})

        assert results == []

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_cache_hit_skips_llm(self, mock_llm):
        """缓存命中时不应调用 LLM。"""
        # 预填充缓存
        _cache_set("花之魔术师", "梅林")

        from server.query_executor import load_database

        db = load_database()
        skill = SKILL_REGISTRY["resolve_nickname"]
        results = skill.execute(db, {"name": "花之魔术师"})

        assert len(results) >= 1
        mock_llm.assert_not_called()

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_successful_resolve_populates_cache(self, mock_llm):
        """成功识别后应写入缓存。"""
        mock_llm.return_value = "梅林"

        from server.query_executor import load_database

        db = load_database()
        skill = SKILL_REGISTRY["resolve_nickname"]
        skill.execute(db, {"name": "花之魔术师"})

        # 验证缓存已写入
        from server.skills.query.resolve_nickname import _normalize_text

        cache_key = _normalize_text("花之魔术师")
        hit, value = _cache_get(cache_key)
        assert hit is True
        assert value == "梅林"

    def test_empty_name_returns_empty(self):
        """空名称参数应直接返回空结果。"""
        from server.query_executor import load_database

        db = load_database()
        skill = SKILL_REGISTRY["resolve_nickname"]
        results = skill.execute(db, {"name": ""})
        assert results == []


class TestExecutorFallbackChain:
    """测试 SkillExecutor 的 nickname resolve fallback 链。"""

    def setup_method(self):
        self.executor = SkillExecutor()
        _nickname_cache.clear()

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_fallback_triggers_on_lookup_empty(self, mock_llm):
        """lookup_servant 单独查空时应触发 resolve_nickname fallback。"""
        mock_llm.return_value = "梅林"

        result = self.executor.execute(
            skill_calls=[{"skill_name": "lookup_servant", "params": {"name": "花之魔术师"}}],
        )

        # 应该通过 fallback 找到梅林
        assert result.total_found >= 1
        assert not result.is_fallback
        # accepted_skills 中应包含 resolve_nickname
        skill_names = [s["skill_name"] for s in result.accepted_skills]
        assert "resolve_nickname" in skill_names

    def test_fallback_not_triggered_on_combo_query(self):
        """组合筛选（多个 Skill）时不应触发 fallback。"""
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "lookup_servant", "params": {"name": "绝对不存在的从者XYZ"}},
                {"skill_name": "search_by_class", "params": {"className": "Caster"}},
            ],
        )

        # 组合查询不触发 resolve_nickname
        skill_names = [s["skill_name"] for s in result.accepted_skills]
        assert "resolve_nickname" not in skill_names

    def test_fallback_not_triggered_on_non_lookup(self):
        """非 lookup_servant 的单 Skill 空结果不应触发 fallback。"""
        result = self.executor.execute(
            skill_calls=[
                {"skill_name": "search_by_class", "params": {"className": "NotAClass"}},
            ],
        )

        # 不是 lookup_servant，不触发
        skill_names = [s["skill_name"] for s in result.accepted_skills]
        assert "resolve_nickname" not in skill_names

    @patch("server.skills.query.resolve_nickname.ResolveNickname._call_llm_sync")
    def test_fallback_returns_empty_when_llm_fails(self, mock_llm):
        """LLM 识别失败时，fallback 应正常返回空结果。"""
        mock_llm.return_value = None

        result = self.executor.execute(
            skill_calls=[{"skill_name": "lookup_servant", "params": {"name": "完全不存在"}}],
        )

        assert result.total_found == 0
        assert result.is_fallback
