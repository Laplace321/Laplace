"""概念礼装查询模块测试。"""

import json
from pathlib import Path

import pytest


@pytest.fixture
def ce_db():
    """加载礼装数据库（跳过如果数据不存在）。"""
    db_path = Path(__file__).parent.parent / "server" / "data" / "craft_essences_db.json"
    if not db_path.exists():
        pytest.skip("craft_essences_db.json 不存在，跳过礼装测试")
    with open(db_path, encoding="utf-8") as f:
        return json.load(f)


class TestCEDatabase:
    """验证礼装数据库构建的正确性。"""

    def test_database_not_empty(self, ce_db):
        assert len(ce_db) > 2000

    def test_kaleidoscope_np_charge(self, ce_db):
        """万花筒：满破 NP 100%，纯攻型（atkMax=2000, hpMax=0）。"""
        kscope = next((ce for ce in ce_db if ce["name"] == "Kaleidoscope"), None)
        assert kscope is not None
        assert kscope["npChargePercent"] == 100
        assert kscope["nameCn"] == "万华镜"
        assert kscope["rarity"] == 5
        assert kscope["atkType"] == "pure_atk"
        assert "gainNp" in kscope["effectsLimitBreak"]

    def test_heavens_feel_pure_atk(self, ce_db):
        """黑圣杯：纯攻型。"""
        hf = next((ce for ce in ce_db if ce["name"] == "Heaven's Feel"), None)
        assert hf is not None
        assert hf["atkType"] == "pure_atk"
        assert hf["nameCn"] == "天之杯"

    def test_obtain_classification(self, ce_db):
        """验证获取方式分类合理。"""
        from collections import Counter

        obtains = Counter(ce["obtain"] for ce in ce_db)
        # 应该有多种获取方式
        assert len(obtains) >= 5
        # 限定数量应大于常驻
        assert obtains["limited"] > obtains["permanent"]
        # bond 和 valentine 数量合理
        assert obtains["bond"] > 400
        assert obtains["valentine"] > 400

    def test_face_url_format(self, ce_db):
        """验证头像 URL 格式。"""
        for ce in ce_db[:10]:
            if ce.get("faceUrl"):
                assert ce["faceUrl"].startswith("/faces/")
                assert ce["faceUrl"].endswith(".png")
            if ce.get("_faceUrlSource"):
                assert ce["_faceUrlSource"].startswith("https://")


class TestCESkills:
    """测试 CE Query Skills 的执行逻辑。"""

    def test_ce_lookup_by_nickname(self, ce_db):
        """通过昵称查找礼装。"""
        from server.skills.query.ce_lookup import CELookup

        skill = CELookup()
        results = skill.execute(ce_db, {"name": "万花筒"})
        assert len(results) >= 1
        assert results[0]["name"] == "Kaleidoscope"

    def test_ce_lookup_by_english_name(self, ce_db):
        """通过英文名查找礼装。"""
        from server.skills.query.ce_lookup import CELookup

        skill = CELookup()
        results = skill.execute(ce_db, {"name": "Kaleidoscope"})
        assert len(results) == 1

    def test_ce_search_by_effect(self, ce_db):
        """按效果搜索礼装。"""
        from server.skills.query.ce_search_by_effect import CESearchByEffect

        skill = CESearchByEffect()
        results = skill.execute(ce_db, {"effect": "gainNp", "limit_break": True})
        assert len(results) > 0
        # 万花筒应在结果中
        names = [ce["name"] for ce in results]
        assert "Kaleidoscope" in names

    def test_ce_search_by_rarity(self, ce_db):
        """按稀有度筛选礼装。"""
        from server.skills.query.ce_search_by_rarity import CESearchByRarity

        skill = CESearchByRarity()
        results = skill.execute(ce_db, {"op": "eq", "value": 5})
        assert len(results) > 0
        assert all(ce["rarity"] == 5 for ce in results)

    def test_ce_search_by_atk_type(self, ce_db):
        """按 ATK 类型筛选礼装。"""
        from server.skills.query.ce_search_by_atk_type import CESearchByAtkType

        skill = CESearchByAtkType()
        results = skill.execute(ce_db, {"atk_type": "pure_atk"})
        assert len(results) > 0
        assert all(ce["atkType"] == "pure_atk" for ce in results)

    def test_ce_search_by_obtain(self, ce_db):
        """按获取方式筛选礼装。"""
        from server.skills.query.ce_search_by_obtain import CESearchByObtain

        skill = CESearchByObtain()
        results = skill.execute(ce_db, {"obtain_type": "bond"})
        assert len(results) > 400
        assert all(ce["obtain"] == "bond" for ce in results)

    def test_ce_search_by_obtain_chinese_alias(self, ce_db):
        """中文别名解析获取方式。"""
        from server.skills.query.ce_search_by_obtain import CESearchByObtain

        skill = CESearchByObtain()
        results = skill.execute(ce_db, {"obtain_type": "羁绊"})
        assert len(results) > 400


class TestCEContextBuilder:
    """测试礼装 Context 构建。"""

    def test_build_ce_context(self, ce_db):
        """构建礼装 context 应正确生成中文化数据。"""
        from server.context_builder import build_ce_context

        context_data, top_results = build_ce_context(ce_db[:10])
        assert context_data["匹配总数"] == 10
        assert len(top_results) <= 5
        # 验证中文化
        for entry in top_results:
            assert "名称" in entry
            assert "中文名" in entry
            assert "稀有度" in entry
            assert "类型" in entry


class TestCEDescribeFilters:
    """测试 describe_filters 对 CE skills 的中文描述。"""

    def test_ce_lookup_description(self):
        from server.translation import describe_filters

        result = describe_filters([{"skill_name": "ce_lookup", "params": {"name": "万花筒"}}])
        assert "查询礼装「万花筒」" in result

    def test_ce_effect_description(self):
        from server.translation import describe_filters

        result = describe_filters([{"skill_name": "ce_search_by_effect", "params": {"effect": "gainNp"}}])
        assert "礼装效果" in result[0]
        assert "满破" in result[0]

    def test_ce_rarity_description(self):
        from server.translation import describe_filters

        result = describe_filters([{"skill_name": "ce_search_by_rarity", "params": {"op": "eq", "value": 5}}])
        assert "礼装稀有度" in result[0]
        assert "5星" in result[0]
