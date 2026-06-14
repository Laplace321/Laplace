"""Pipeline C 多轮上下文注入回归测试（trace b69a2c90）。

覆盖 ``_extract_prev_servant_context`` 与 ``_build_guide_generation_prompt`` 的关键分支：
- MAJOR / 无 prev_turn / 无 servants 时不注入实体
- MINOR + prev_turn + servants 时回写主从者名，并改写检索 query
- 用户原句已显式包含主从者名时不重复拼接
- generation_prompt 仅在 prev_servant_label 非 None 时含「上下文」段
"""

from __future__ import annotations

from server.graph.session import TurnSnapshot
from server.graph.state import PipelineState
from server.nodes.guide import (
    _build_guide_generation_prompt,
    _extract_prev_servant_context,
)


def _make_state(
    user_message: str,
    turn_type: str = "MAJOR",
    prev_turn: TurnSnapshot | None = None,
) -> PipelineState:
    state = PipelineState(user_message=user_message, turn_type=turn_type)
    if prev_turn is not None:
        state.extras["prev_turn"] = prev_turn
    return state


class TestExtractPrevServantContext:
    def test_major_turn_returns_none(self):
        prev = TurnSnapshot(session_id="s1", servants=[{"collectionNo": 1, "name": "水呆"}])
        state = _make_state("狂阶戴冠战如何打", turn_type="MAJOR", prev_turn=prev)
        query, label = _extract_prev_servant_context(state)
        assert query == "狂阶戴冠战如何打"
        assert label is None

    def test_minor_without_prev_turn_returns_none(self):
        state = _make_state("那这个角色狂阶戴冠战如何组队", turn_type="MINOR")
        query, label = _extract_prev_servant_context(state)
        assert query == "那这个角色狂阶戴冠战如何组队"
        assert label is None

    def test_minor_with_empty_servants_returns_none(self):
        prev = TurnSnapshot(session_id="s1", servants=[])
        state = _make_state("那这个角色如何打", turn_type="MINOR", prev_turn=prev)
        _, label = _extract_prev_servant_context(state)
        assert label is None

    def test_minor_with_servants_injects_name(self):
        """trace b69a2c90 回归：MINOR 续问 + prev servants 必须改写检索 query 并返回从者名。"""
        prev = TurnSnapshot(
            session_id="s1",
            servants=[{"collectionNo": 268, "name": "水着阿尔托莉雅·水呆"}],
        )
        state = _make_state("那这个角色在狂阶戴冠战如何组队", turn_type="MINOR", prev_turn=prev)
        query, label = _extract_prev_servant_context(state)
        assert label == "水着阿尔托莉雅·水呆"
        assert query.startswith("水着阿尔托莉雅·水呆 ")
        assert "那这个角色在狂阶戴冠战如何组队" in query

    def test_minor_skips_concat_when_name_already_in_message(self):
        prev = TurnSnapshot(session_id="s1", servants=[{"collectionNo": 268, "name": "水呆"}])
        state = _make_state("水呆在狂阶戴冠战如何组队", turn_type="MINOR", prev_turn=prev)
        query, label = _extract_prev_servant_context(state)
        assert label == "水呆"
        # 没有重复拼接，避免「水呆 水呆 在...」
        assert query == "水呆在狂阶戴冠战如何组队"

    def test_correction_turn_also_injects(self):
        prev = TurnSnapshot(session_id="s1", servants=[{"collectionNo": 268, "name": "水呆"}])
        state = _make_state("我说的是泳装版本", turn_type="CORRECTION", prev_turn=prev)
        _, label = _extract_prev_servant_context(state)
        assert label == "水呆"


class TestBuildGuideGenerationPrompt:
    def test_no_prev_servant_no_context_section(self):
        prompt = _build_guide_generation_prompt(
            guide_context="【戴冠战 Berserker】\n核心打法：摩根 + 水呆。",
            user_message="狂阶戴冠战如何组队",
        )
        assert "## 上下文" not in prompt
        assert "## 知识范围" in prompt

    def test_with_prev_servant_includes_context_section(self):
        """trace b69a2c90 回归：上下文段必须显式提示「这个从者」指代上一轮的从者名。"""
        prompt = _build_guide_generation_prompt(
            guide_context="【戴冠战 Berserker】\n核心打法：摩根 + 水呆。",
            user_message="那这个角色如何组队",
            prev_servant_label="水呆",
        )
        assert "## 上下文" in prompt
        assert "「水呆」" in prompt
        assert "这个从者 / 这个角色 / TA" in prompt
        # 业务约束：禁止 LLM 反问
        assert "禁止反问" in prompt
