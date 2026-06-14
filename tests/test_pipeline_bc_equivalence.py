"""Pipeline B/C 通过图引擎执行的等价性测试（Task 1）。

目标：在不调用真实 LLM 的前提下，验证 ``_handle_atlas_pipeline`` /
``_handle_guide_pipeline`` 经过图引擎包装后，行为与原命令式实现完全一致。
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

# ────────────────────────────────────────────────────────────
# Pipeline B (Atlas) 等价性
# ────────────────────────────────────────────────────────────


class _FakeAtlas:
    def __init__(self, results: list[dict]):
        self._results = results

    def search(self, params: Any) -> list[dict]:
        return self._results

    def verify_fact(self, *_args, **_kwargs) -> bool:
        return True


@pytest.mark.asyncio
async def test_atlas_pipeline_no_match_returns_canned_reply():
    from server import pipeline

    fake_atlas = _FakeAtlas(results=[])

    with (
        patch("server.nodes.atlas.get_atlas_index", return_value=fake_atlas),
    ):
        resp = await pipeline._handle_atlas_pipeline(
            user_message="梅林什么时候复刻",
            trace_id="t1",
            model_used="m",
            request_start=time.monotonic(),
            trace_total_tokens=0,
            atlas_query={"name": "梅林", "entry_type": "gacha"},
        )

    assert "未找到相关信息" in resp.reply
    assert resp.servants == []
    assert resp.count == 0
    assert resp.query == {"mode": "atlas_pipeline"}
    assert resp.traceId == "t1"


@pytest.mark.asyncio
async def test_atlas_pipeline_with_results_runs_generation():
    from server import pipeline

    fake_atlas = _FakeAtlas(
        results=[
            {"entry_key": "k1", "name": "梅林复刻", "type": "gacha", "year_month": "2025-07"},
        ]
    )

    async def fake_chat_completion(**kwargs):
        return {
            "text": "梅林复刻预计 2025 年 7 月。",
            "_usage": {"total_tokens": 100},
            "_model": "fake-model",
        }

    with (
        patch("server.nodes.atlas.get_atlas_index", return_value=fake_atlas),
        patch("server.nodes.atlas.chat_completion", side_effect=fake_chat_completion),
    ):
        resp = await pipeline._handle_atlas_pipeline(
            user_message="梅林什么时候复刻",
            trace_id="t2",
            model_used="initial",
            request_start=time.monotonic(),
            trace_total_tokens=0,
            atlas_query={"name": "梅林"},
        )

    assert resp.reply == "梅林复刻预计 2025 年 7 月。"
    assert resp.model == "fake-model"
    assert resp.query == {"mode": "atlas_pipeline"}
    assert resp.count == 0


@pytest.mark.asyncio
async def test_atlas_pipeline_extracts_query_when_missing():
    """无 atlas_query 时应触发 _extract_atlas_query LLM 调用。"""
    from server import pipeline

    extraction_call_count = {"n": 0}

    async def fake_extract(user_message: str, trace_id: str):
        extraction_call_count["n"] += 1
        return {"name": "周年庆", "entry_type": "event"}

    fake_atlas = _FakeAtlas(results=[{"entry_key": "k", "name": "周年庆活动", "type": "event"}])

    async def fake_chat_completion(**kwargs):
        return {"text": "周年庆是 7 月。", "_usage": {}, "_model": "m"}

    with (
        patch("server.nodes.atlas.get_atlas_index", return_value=fake_atlas),
        patch("server.nodes.atlas._extract_atlas_query", side_effect=fake_extract),
        patch("server.nodes.atlas.chat_completion", side_effect=fake_chat_completion),
    ):
        resp = await pipeline._handle_atlas_pipeline(
            user_message="周年庆什么时候",
            trace_id="t3",
            model_used="initial",
            request_start=time.monotonic(),
            trace_total_tokens=0,
            atlas_query=None,
        )

    assert extraction_call_count["n"] == 1
    assert "7 月" in resp.reply


# ────────────────────────────────────────────────────────────
# Pipeline C (Guide) 等价性
# ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guide_pipeline_no_match_returns_canned_reply():
    from server import pipeline

    with patch("server.nodes.guide._prepare_guide_context", return_value=None):
        resp = await pipeline._handle_guide_pipeline(
            user_message="某个冷门关卡",
            trace_id="g1",
            model_used="m",
            request_start=time.monotonic(),
            trace_total_tokens=0,
        )

    assert "未找到相关内容" in resp.reply
    assert resp.servants == []
    assert resp.count == 0
    assert resp.query == {"mode": "guide_pipeline"}


@pytest.mark.asyncio
async def test_guide_pipeline_with_results_appends_source_suffix():
    from server import pipeline

    class _FakeChunk:
        def __init__(self, content: str, metadata: dict):
            self.content = content
            self.metadata = metadata

    chunks = [
        _FakeChunk(
            content="弓阶戴冠战推荐使用 BB",
            metadata={"title": "戴冠战攻略", "section": "弓阶", "author": "某作者"},
        ),
    ]

    async def fake_chat_completion(**kwargs):
        return {
            "text": "弓阶戴冠战核心从者是 BB。",
            "_usage": {"total_tokens": 50},
            "_model": "fake-gen-model",
        }

    with (
        patch(
            "server.nodes.guide._prepare_guide_context",
            return_value=(chunks, {"戴冠战攻略"}, {"戴冠战攻略": "某作者"}),
        ),
        patch("server.nodes.guide.chat_completion", side_effect=fake_chat_completion),
    ):
        resp = await pipeline._handle_guide_pipeline(
            user_message="戴冠战弓阶推荐",
            trace_id="g2",
            model_used="initial",
            request_start=time.monotonic(),
            trace_total_tokens=0,
        )

    assert resp.reply.startswith("弓阶戴冠战核心从者是 BB。")
    assert "📖 参考：戴冠战攻略（作者：**某作者**）" in resp.reply
    assert resp.model == "fake-gen-model"
    assert resp.query == {"mode": "guide_pipeline"}


# ────────────────────────────────────────────────────────────
# 图实例缓存
# ────────────────────────────────────────────────────────────


def test_pipeline_b_graph_is_cached():
    from server import pipeline

    g1 = pipeline._get_pipeline_b_graph()
    g2 = pipeline._get_pipeline_b_graph()
    assert g1 is g2


def test_pipeline_c_graph_is_cached():
    from server import pipeline

    g1 = pipeline._get_pipeline_c_graph()
    g2 = pipeline._get_pipeline_c_graph()
    assert g1 is g2


def test_pipeline_b_graph_topology():
    from server import pipeline
    from server.graph.engine import END

    g = pipeline._build_pipeline_b_graph()
    assert g._entry == "atlas"
    assert g._static_edges == {"atlas": END}


def test_pipeline_c_graph_topology():
    from server import pipeline
    from server.graph.engine import END

    g = pipeline._build_pipeline_c_graph()
    assert g._entry == "guide"
    assert g._static_edges == {"guide": END}
