"""攻略文档检索引擎单元测试。"""

import pytest

from server.guide_retriever import GuideRetriever


@pytest.fixture
def retriever(tmp_path):
    """创建临时攻略目录并写入多个测试文档（BM25 需要足够语料）。"""
    guide1 = """\
---
title: "Saber 攻略"
tags: [coronation, saber]
---

## 配队推荐

Saber 职阶推荐搭配梅林和孔明作为辅助。

## Boss 应对

第一关 Boss 弱点是骑阶，建议使用克制从者。
"""
    guide2 = """\
---
title: "Archer 攻略"
tags: [coronation, archer]
---

## 配队推荐

Archer 职阶推荐搭配 CBA 和花嫁尼禄。

## 素材刷取

每日首通可获得弓之辉石，推荐周回第三关。
"""
    guide3 = """\
---
title: "通用机制说明"
tags: [guide, 机制]
---

## 戴冠战基本规则

戴冠战分为七个职阶关卡和一个 EX 关卡。每个职阶只能使用对应职阶从者。

## 奖励说明

通关后可获得稀有素材和圣晶石奖励。
"""
    (tmp_path / "saber.md").write_text(guide1, encoding="utf-8")
    (tmp_path / "archer.md").write_text(guide2, encoding="utf-8")
    (tmp_path / "general.md").write_text(guide3, encoding="utf-8")
    return GuideRetriever(guides_dir=tmp_path)


class TestGuideRetrieverLoad:
    def test_chunks_loaded(self, retriever):
        assert retriever.chunk_count >= 4

    def test_frontmatter_parsed(self, retriever):
        saber_chunks = [c for c in retriever._chunks if c.metadata.get("title") == "Saber 攻略"]
        assert len(saber_chunks) >= 1
        assert "saber" in saber_chunks[0].metadata.get("tags", [])

    def test_section_extracted(self, retriever):
        sections = [c.metadata.get("section") for c in retriever._chunks]
        assert "配队推荐" in sections
        assert "Boss 应对" in sections


class TestGuideRetrieverSearch:
    def test_search_by_keyword(self, retriever):
        results = retriever.search("Saber 配队")
        assert len(results) >= 1
        assert any("Saber" in r.content or "saber" in r.content.lower() for r in results)

    def test_search_by_tag_filter(self, retriever):
        results = retriever.search("配队", tags=["archer"])
        assert len(results) >= 1
        assert any("Archer" in r.content or "archer" in r.content.lower() for r in results)

    def test_search_no_match(self, retriever):
        results = retriever.search("完全不相关的关键词xyz")
        assert len(results) == 0 or all(r.score <= 0 for r in results)

    def test_search_empty_query(self, retriever):
        results = retriever.search("")
        assert results == []

    def test_doc_level_full_return(self, retriever):
        """文档级检索应返回命中文档的全部 chunk。"""
        results = retriever.search("Saber 配队")
        saber_results = [r for r in results if r.metadata.get("title") == "Saber 攻略"]
        # 应返回 Saber 攻略的所有章节（配队推荐 + Boss 应对）
        assert len(saber_results) >= 2
        sections = {r.metadata.get("section") for r in saber_results}
        assert "配队推荐" in sections
        assert "Boss 应对" in sections

    def test_synonym_recall_via_doc_level(self, retriever):
        """文档级 BM25 通过文档内其他 chunk 的关键词提升整体分数。"""
        # "打手" 不在任何 chunk 标题中，但 Saber 攻略含 "Saber" 关键词
        results = retriever.search("Saber 打手分析")
        saber_results = [r for r in results if r.metadata.get("title") == "Saber 攻略"]
        assert len(saber_results) >= 1

    def test_top_k_is_doc_count(self, retriever):
        """top_k 控制返回文档数而非 chunk 数。"""
        results = retriever.search("配队", top_k=1)
        sources = {r.metadata.get("source") for r in results}
        assert len(sources) <= 1


class TestGuideRetrieverEmptyDir:
    def test_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty_guides"
        empty_dir.mkdir()
        retriever = GuideRetriever(guides_dir=empty_dir)
        assert retriever.chunk_count == 0
        assert retriever.search("anything") == []

    def test_nonexistent_directory(self, tmp_path):
        retriever = GuideRetriever(guides_dir=tmp_path / "nonexistent")
        assert retriever.chunk_count == 0
