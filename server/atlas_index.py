"""Atlas CN 倒排索引 — runtime 查询接口（链路 B）。

采用懒加载单例模式：首次调用 get_atlas_index() 时触发数据拉取和索引构建，
后续调用直接返回内存缓存。不阻塞服务启动。
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from server.data_loader import OUTPUT_DIR, _fetch_atlas_cn, build_atlas_index

INDEX_PATH = OUTPUT_DIR / "atlas_index.json"


class AtlasQueryParams(BaseModel):
    """链路 B 结构化查询参数（由路由 LLM 输出）。"""

    name: str | None = Field(default=None, description="名称关键词（活动/关卡/卡池/素材名）")
    entry_type: Literal["event", "war", "gacha", "item"] | None = Field(default=None, description="条目类型")
    tag: str | None = Field(default=None, description="标签过滤（如 event_type:campaign）")
    year_month: str | None = Field(default=None, description="时间过滤 YYYY-MM")
    linked_servant_id: int | None = Field(default=None, description="关联从者 ID（反查该从者参与的活动/卡池）")


class AtlasIndex:
    """Atlas CN 数据倒排索引，供链路 B 检索使用。"""

    def __init__(self, index_data: dict) -> None:
        self._name_index: dict[str, list[str]] = index_data.get("name_index", {})
        self._tag_index: dict[str, list[str]] = index_data.get("tag_index", {})
        self._time_index: dict[str, list[str]] = index_data.get("time_index", {})
        self._servant_event_index: dict[str, list[int]] = index_data.get("servant_event_index", {})
        self._servant_gacha_index: dict[str, list[int]] = index_data.get("servant_gacha_index", {})
        self._summary_map: dict[str, dict] = index_data.get("summary_map", {})

    def search(self, params: AtlasQueryParams) -> list[dict]:
        """结构化字段匹配，返回匹配的 Atlas 条目摘要列表。"""
        candidate_sets: list[set[str]] = []

        # 名称匹配
        if params.name:
            name_hits = set()
            for token in params.name.split():
                token = token.strip()
                if token and len(token) >= 2:
                    name_hits.update(self._name_index.get(token, []))
            # 完整名称精确匹配
            name_hits.update(self._name_index.get(params.name, []))
            if name_hits:
                candidate_sets.append(name_hits)

        # 类型过滤
        if params.entry_type:
            type_prefix = f"{params.entry_type}:"
            type_hits = {k for k in self._summary_map if k.startswith(type_prefix)}
            if type_hits:
                candidate_sets.append(type_hits)

        # 标签过滤
        if params.tag:
            tag_hits = set(self._tag_index.get(params.tag, []))
            if tag_hits:
                candidate_sets.append(tag_hits)

        # 时间过滤
        if params.year_month:
            time_hits = set(self._time_index.get(params.year_month, []))
            if time_hits:
                candidate_sets.append(time_hits)

        # 关联从者反查
        if params.linked_servant_id is not None:
            svt_id_str = str(params.linked_servant_id)
            servant_hits: set[str] = set()
            if params.entry_type in (None, "event"):
                for eid in self._servant_event_index.get(svt_id_str, []):
                    servant_hits.add(f"event:{eid}")
            if params.entry_type in (None, "gacha"):
                for gid in self._servant_gacha_index.get(svt_id_str, []):
                    servant_hits.add(f"gacha:{gid}")
            if servant_hits:
                candidate_sets.append(servant_hits)

        # 取交集
        if not candidate_sets:
            return []

        # 防止低选择性查询返回全量结果：如果只有 entry_type 一个维度的候选集且数量过大，
        # 说明 name/servant 等高选择性条件未命中任何数据，仅按类型过滤会返回过多结果。
        # 此时视为无效查询，返回空结果触发 raw_fallback。
        _LOW_SELECTIVITY_THRESHOLD = 50
        if len(candidate_sets) == 1 and params.entry_type and len(candidate_sets[0]) > _LOW_SELECTIVITY_THRESHOLD:
            return []

        result_keys = candidate_sets[0]
        for cs in candidate_sets[1:]:
            result_keys = result_keys & cs

        # 按 summary_map 顺序返回摘要
        results = []
        for key in sorted(result_keys):
            summary = self._summary_map.get(key)
            if summary:
                results.append({"entry_key": key, **summary})
        return results

    def get_detail(self, entry_key: str) -> dict | None:
        """按 entry_key 获取单条摘要数据。"""
        return self._summary_map.get(entry_key)

    def verify_fact(self, entity_type: str, entity_value: str) -> bool:
        """轻量级事实验证：检查实体是否存在于索引中。

        Args:
            entity_type: "name" | "tag" | "time"
            entity_value: 要验证的值
        """
        if entity_type == "name":
            return entity_value in self._name_index
        if entity_type == "tag":
            return entity_value in self._tag_index
        if entity_type == "time":
            return entity_value in self._time_index
        return False

    @property
    def entry_count(self) -> int:
        return len(self._summary_map)


# ── 懒加载单例 ──

_atlas_index_instance: AtlasIndex | None = None


def get_atlas_index() -> AtlasIndex:
    """获取 Atlas 索引单例。首次调用时触发数据拉取和索引构建。"""
    global _atlas_index_instance
    if _atlas_index_instance is not None:
        return _atlas_index_instance

    # 优先从已构建的索引文件加载
    if INDEX_PATH.exists():
        print("📦 Atlas 索引: 从缓存文件加载")
        with open(INDEX_PATH, encoding="utf-8") as f:
            index_data = json.load(f)
        _atlas_index_instance = AtlasIndex(index_data)
        print(f"   ✅ Atlas 索引就绪: {_atlas_index_instance.entry_count} 条条目")
        return _atlas_index_instance

    # 索引文件不存在，触发拉取 + 构建
    print("🔨 Atlas 索引: 首次构建（拉取 CN 数据 + 构建索引）...")
    atlas_data = _fetch_atlas_cn()
    if not any(atlas_data.values()):
        print("⚠️  Atlas CN 数据为空，创建空索引")
        _atlas_index_instance = AtlasIndex({})
        return _atlas_index_instance

    index_data = build_atlas_index(atlas_data)
    _atlas_index_instance = AtlasIndex(index_data)
    print(f"   ✅ Atlas 索引就绪: {_atlas_index_instance.entry_count} 条条目")
    return _atlas_index_instance
