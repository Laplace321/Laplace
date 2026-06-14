"""ADR-031 _compute_servant_briefs 单元测试 — 验证 A→C 上下文注入的触发条件。

覆盖：
- 仅 Pipeline A 命中且 servants ≤ 3 时生成 briefs
- B / C 链路或 servants > 3 时返回空列表
- 空 returned_servants 返回空列表
"""

from __future__ import annotations

from server.graph.state import PipelineState
from server.nodes.generate import _compute_servant_briefs


def _state_with_pipeline(pipeline: str) -> PipelineState:
    s = PipelineState(user_message="test")
    s.classified_pipeline = pipeline
    return s


def _make_servant(name: str = "测试") -> dict:
    return {
        "name": "Test",
        "aliasCN": name,
        "className": "saber",
        "rarity": 5,
        "cards": "BAAAQ",
        "npCard": "buster",
        "npTarget": "enemy",
        "totalSelfCharge": 30,
    }


class TestComputeServantBriefs:
    def test_pipeline_a_with_one_servant_generates_brief(self):
        state = _state_with_pipeline("A")
        briefs = _compute_servant_briefs(state, [_make_servant("水呆")])
        assert len(briefs) == 1
        assert "水呆" in briefs[0]

    def test_pipeline_a_with_three_servants_generates_three_briefs(self):
        state = _state_with_pipeline("A")
        briefs = _compute_servant_briefs(state, [_make_servant("A"), _make_servant("B"), _make_servant("C")])
        assert len(briefs) == 3

    def test_pipeline_a_with_four_servants_returns_empty(self):
        """servants > 3 时不生成 briefs，避免 token 失控。"""
        state = _state_with_pipeline("A")
        briefs = _compute_servant_briefs(state, [_make_servant(f"S{i}") for i in range(4)])
        assert briefs == []

    def test_pipeline_b_returns_empty(self):
        state = _state_with_pipeline("B")
        briefs = _compute_servant_briefs(state, [_make_servant()])
        assert briefs == []

    def test_pipeline_c_returns_empty(self):
        state = _state_with_pipeline("C")
        briefs = _compute_servant_briefs(state, [_make_servant()])
        assert briefs == []

    def test_empty_servants_returns_empty(self):
        state = _state_with_pipeline("A")
        assert _compute_servant_briefs(state, []) == []

    def test_skips_non_dict_servants(self):
        state = _state_with_pipeline("A")
        briefs = _compute_servant_briefs(state, [_make_servant("OK"), "not a dict"])  # type: ignore[list-item]
        assert len(briefs) == 1
        assert "OK" in briefs[0]
