"""攻略文档混合检索引擎（链路 C）— 文档级 BM25 + 全文传入 + 向量兜底。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from rank_bm25 import BM25Okapi


@dataclass
class GuideChunk:
    """攻略文档块。"""

    content: str
    metadata: dict = field(default_factory=dict)
    score: float = 0.0


class GuideRetriever:
    """攻略文档混合检索引擎（文档级 BM25 + 全文传入 + 向量兜底）。"""

    def __init__(self, guides_dir: Path | None = None) -> None:
        if guides_dir is None:
            guides_dir = Path(__file__).parent / "data" / "guides"
        self._guides_dir = guides_dir
        self._chunks: list[GuideChunk] = []
        self._doc_chunks: dict[str, list[int]] = {}  # source → [chunk_indices]
        self._bm25: BM25Okapi | None = None
        self._tokenized_corpus: list[list[str]] = []
        self._load()

    def _load(self) -> None:
        """加载 guides/*.md，切分文档块，构建 BM25 索引。"""
        if not self._guides_dir.exists():
            print(f"⚠️  攻略目录不存在: {self._guides_dir}")
            return

        md_files = sorted(self._guides_dir.glob("*.md"))
        if not md_files:
            print(f"⚠️  攻略目录为空: {self._guides_dir}")
            return

        for md_file in md_files:
            raw = md_file.read_text(encoding="utf-8")
            metadata, body = self._parse_frontmatter(raw)
            metadata["source"] = md_file.name
            chunks = self._split_document(body, metadata)
            start_idx = len(self._chunks)
            self._chunks.extend(chunks)
            self._doc_chunks[md_file.name] = list(range(start_idx, start_idx + len(chunks)))

        # 构建 BM25 索引
        self._tokenized_corpus = [self._tokenize(c.content) for c in self._chunks]
        if self._tokenized_corpus:
            self._bm25 = BM25Okapi(self._tokenized_corpus)

        print(f"📚 攻略检索引擎就绪: {len(md_files)} 个文档, {len(self._chunks)} 个文档块")

    @staticmethod
    def _parse_frontmatter(raw: str) -> tuple[dict, str]:
        """解析 YAML frontmatter，返回 (metadata, body)。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.DOTALL)
        if not match:
            return {}, raw
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata, match.group(2)

    @staticmethod
    def _split_document(body: str, metadata: dict) -> list[GuideChunk]:
        """按 ## 标题切分文档为 ~300-500 token 的块。"""
        sections = re.split(r"(?=^## )", body, flags=re.MULTILINE)
        chunks: list[GuideChunk] = []
        for section in sections:
            section = section.strip()
            if not section:
                continue
            chunk_meta = {**metadata}
            # 提取小节标题
            title_match = re.match(r"^## (.+)$", section, re.MULTILINE)
            if title_match:
                chunk_meta["section"] = title_match.group(1).strip()
            chunks.append(GuideChunk(content=section, metadata=chunk_meta))
        # 如果没有任何 ## 分割，整篇作为一个块
        if not chunks and body.strip():
            chunks.append(GuideChunk(content=body.strip(), metadata=metadata))
        return chunks

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简易中文分词：按非中文/非字母数字字符分割，保留连续中文和英文单词。"""
        tokens = re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", text.lower())
        expanded: list[str] = []
        for token in tokens:
            # 始终保留原始 token
            expanded.append(token)
            # 对长中文串额外做 bigram 切分以提升召回
            if re.match(r"^[\u4e00-\u9fff]+$", token) and len(token) > 2:
                for i in range(len(token) - 1):
                    expanded.append(token[i : i + 2])
        return expanded

    def search(self, query: str, tags: list[str] | None = None, top_k: int = 3) -> list[GuideChunk]:
        """文档级 BM25 检索：按文档聚合分数，返回命中文档全文。"""
        if not self._chunks or self._bm25 is None:
            return []

        query_tokens = self._tokenize(query)
        scores = self._bm25.get_scores(query_tokens)

        # tag 过滤
        if tags:
            tag_set = set(tags)
            for i, chunk in enumerate(self._chunks):
                chunk_tags = set(chunk.metadata.get("tags", []))
                if not chunk_tags & tag_set:
                    scores[i] = 0.0

        # 文档级评分：同一文档所有 chunk 取 max 分数
        doc_scores: dict[str, float] = {}
        for source, indices in self._doc_chunks.items():
            max_score = max((scores[i] for i in indices), default=0.0)
            if max_score > 0:
                doc_scores[source] = max_score

        # 按文档分数排序，取 top_k 篇文档
        top_docs = sorted(doc_scores.keys(), key=lambda s: doc_scores[s], reverse=True)[:top_k]

        # 返回命中文档的全部 chunk（全文传入）
        results: list[GuideChunk] = []
        for source in top_docs:
            for idx in self._doc_chunks[source]:
                chunk = self._chunks[idx]
                chunk.score = float(scores[idx])
                results.append(chunk)

        # 向量兜底：当 BM25 无结果时触发（预留）
        if not results:
            vector_results = self._vector_search(query, tags, top_k)
            if vector_results:
                results = vector_results

        return results

    def _vector_search(self, query: str, tags: list[str] | None = None, top_k: int = 3) -> list[GuideChunk]:
        """向量语义检索（一期预留接口，返回空列表）。"""
        return []

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)
