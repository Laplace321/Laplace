"""Atlas 倒排索引单元测试。"""

import pytest

from server.atlas_index import AtlasIndex, AtlasQueryParams


@pytest.fixture
def sample_index():
    """构建一个小型测试索引。"""
    index_data = {
        "name_index": {
            "冬木": ["war:100"],
            "特异点F": ["war:100"],
            "燃烧污染都市": ["war:100"],
            "圣诞": ["event:80001", "event:80002"],
            "万圣节": ["event:80003"],
        },
        "tag_index": {
            "war_flag:mainScenario": ["war:100"],
            "event_type:campaign": ["event:80001"],
            "event_type:loginBonus": ["event:80002"],
        },
        "time_index": {
            "2024-03": ["event:80001"],
            "2024-10": ["event:80003"],
        },
        "servant_event_index": {
            "1": [80001],
            "2": [80001, 80003],
        },
        "servant_gacha_index": {
            "1": [100],
        },
        "summary_map": {
            "war:100": {"类型": "关卡", "名称": "特异点F", "全名": "特异点F 燃烧污染都市 冬木"},
            "event:80001": {"类型": "活动", "名称": "圣诞活动", "开始时间": "2024-03"},
            "event:80002": {"类型": "活动", "名称": "圣诞登录奖励"},
            "event:80003": {"类型": "活动", "名称": "万圣节活动", "开始时间": "2024-10"},
        },
    }
    return AtlasIndex(index_data)


class TestAtlasIndexSearch:
    def test_search_by_name(self, sample_index):
        params = AtlasQueryParams(name="冬木")
        results = sample_index.search(params)
        assert len(results) == 1
        assert results[0]["entry_key"] == "war:100"

    def test_search_by_entry_type(self, sample_index):
        params = AtlasQueryParams(entry_type="event")
        results = sample_index.search(params)
        assert len(results) == 3
        assert all(r["entry_key"].startswith("event:") for r in results)

    def test_search_by_tag(self, sample_index):
        params = AtlasQueryParams(tag="event_type:campaign")
        results = sample_index.search(params)
        assert len(results) == 1
        assert results[0]["entry_key"] == "event:80001"

    def test_search_by_time(self, sample_index):
        params = AtlasQueryParams(year_month="2024-10")
        results = sample_index.search(params)
        assert len(results) == 1
        assert results[0]["entry_key"] == "event:80003"

    def test_search_by_linked_servant(self, sample_index):
        params = AtlasQueryParams(linked_servant_id=2)
        results = sample_index.search(params)
        assert len(results) == 2
        keys = {r["entry_key"] for r in results}
        assert "event:80001" in keys
        assert "event:80003" in keys

    def test_search_intersection(self, sample_index):
        """名称 + 类型交集：'圣诞' AND event → 2 条"""
        params = AtlasQueryParams(name="圣诞", entry_type="event")
        results = sample_index.search(params)
        assert len(results) == 2

    def test_search_no_match(self, sample_index):
        params = AtlasQueryParams(name="不存在的活动")
        results = sample_index.search(params)
        assert results == []

    def test_search_empty_params(self, sample_index):
        params = AtlasQueryParams()
        results = sample_index.search(params)
        assert results == []


class TestAtlasIndexVerifyFact:
    def test_verify_name_exists(self, sample_index):
        assert sample_index.verify_fact("name", "冬木") is True

    def test_verify_name_missing(self, sample_index):
        assert sample_index.verify_fact("name", "不存在") is False

    def test_verify_tag(self, sample_index):
        assert sample_index.verify_fact("tag", "war_flag:mainScenario") is True

    def test_verify_time(self, sample_index):
        assert sample_index.verify_fact("time", "2024-03") is True


class TestAtlasIndexGetDetail:
    def test_get_existing(self, sample_index):
        detail = sample_index.get_detail("war:100")
        assert detail is not None
        assert detail["名称"] == "特异点F"

    def test_get_missing(self, sample_index):
        assert sample_index.get_detail("war:999") is None
