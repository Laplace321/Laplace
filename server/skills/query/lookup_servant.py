"""Skill: 按名称查询单个从者（精确/模糊/昵称）。

改为自定义 execute() 实现，保留所有匹配候选（而非 filter 只返回 bool），
以支持执行层多候选 Clarification 检测。
"""

import re

from pydantic import BaseModel, ConfigDict, Field

from server.query_executor import _normalize_text, load_nicknames
from server.skills.base import QuerySkill, register_skill

# 中文职阶限定词 → className 映射（用于从查询名中剥离职阶前/后缀）。
# 严格只匹配多字词（如"狂阶"、"裁定者"），避免单字词（如"剑"/"狂"）与从者名冲突。
_CN_CLASS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("剑阶", "saber"), ("弓阶", "archer"), ("枪阶", "lancer"),
    ("骑阶", "rider"), ("术阶", "caster"), ("杀阶", "assassin"),
    ("狂阶", "berserker"), ("裁阶", "ruler"), ("复阶", "avenger"),
    ("盾阶", "shielder"), ("兽阶", "beast"),
    ("降阶", "foreigner"), ("伪阶", "pretender"),
    ("裁定者", "ruler"), ("复仇者", "avenger"),
    ("降临者", "foreigner"), ("伪装者", "pretender"),
    ("月癌", "mooncancer"), ("他人格", "alterego"),
)


def _extract_class_constraint(query_name: str) -> tuple[str, str | None]:
    """从查询名中识别中文职阶限定词，返回 (剥离后名称, className 或 None)。

    支持形式：
    - 前缀：「狂阶宫本武藏」→ ("宫本武藏", "berserker")
    - 括号后缀：「宫本武藏（狂阶）」→ ("宫本武藏", "berserker")
    - 星级前缀：「★★★★★ 宫本武藏（狂阶）」→ ("宫本武藏", "berserker")
    """
    name = query_name.strip()
    # 剥离星级前缀（兼容 confirmation_direct 传入的 label）
    name = re.sub(r"^[★☆\s]+", "", name).strip()

    cn_class: str | None = None

    # 1) 括号后缀：「XXX（剑阶）」 / 「XXX(剑阶)」
    m = re.search(r"[（(]([^（）()]+)[)）]\s*$", name)
    if m:
        bracket_content = m.group(1).strip()
        for kw, en_class in _CN_CLASS_KEYWORDS:
            if kw in bracket_content:
                cn_class = en_class
                name = name[: m.start()].strip()
                break

    # 2) 前缀：「狂阶XXX」
    if cn_class is None:
        for kw, en_class in _CN_CLASS_KEYWORDS:
            if name.startswith(kw) and len(name) > len(kw):
                cn_class = en_class
                name = name[len(kw):].strip()
                break

    return name, cn_class


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    name: str = Field(description="从者名称（支持中/英/日/昵称）")


def _resolve_nickname(query_name: str) -> list[tuple[str, dict]]:
    """解析昵称映射，返回所有匹配的 (映射后名称, 额外过滤器) 列表。

    当同一昵称指向多个从者（Mooncell 冲突条目为 list）时，
    返回多组候选，由 find_servant_candidates 收集后交给 Clarification 机制。
    """
    normalized_query = _normalize_text(query_name)
    nicknames = load_nicknames()

    mapped_data = None
    for nick, data in nicknames.items():
        if _normalize_text(nick) == normalized_query:
            mapped_data = data
            break

    if mapped_data is None:
        return []

    # 统一转为列表处理
    entries = mapped_data if isinstance(mapped_data, list) else [mapped_data]

    results = []
    for entry in entries:
        if isinstance(entry, str):
            results.append((entry.lower(), {}))
        elif isinstance(entry, dict):
            name = entry.get("name", "").lower()
            extra_filters = {}
            for key, val in entry.items():
                if key not in ("name", "_source", "_collectionNo"):
                    extra_filters[key] = val
            results.append((name, extra_filters))

    return results


def find_servant_candidates(db: list[dict], query_name: str) -> list[dict]:
    """在数据库中按名称查找所有匹配的从者候选。

    三阶段匹配（精确→子串→反向子串），保留所有匹配结果。
    公开函数，供 lookup_servant 和 compare_servants 共用。
    """
    query_name = query_name.strip()
    if not query_name:
        return []

    # 预处理：识别中文职阶限定词（如"狂阶宫本武藏"/"宫本武藏（狂阶）"）。
    # 剥离职阶词后用仅剩名称部分做模糊匹配，并用 className 二次过滤。
    stripped_name, class_constraint = _extract_class_constraint(query_name)
    if class_constraint and stripped_name and stripped_name != query_name:
        candidates = find_servant_candidates(db, stripped_name)
        return [
            s for s in candidates
            if str(s.get("className", "")).lower() == class_constraint
        ]

    normalized_query = _normalize_text(query_name)
    nickname_mappings = _resolve_nickname(query_name)

    # 阶段 1: 精确匹配（昵称映射后）
    exact_matches: list[dict] = []
    # 阶段 2: 子串模糊匹配
    substring_matches: list[dict] = []
    # 阶段 3: 反向子串匹配
    reverse_matches: list[dict] = []

    # 收集所有昵称映射的 extra_filters（用于多候选时）
    # 如果有多组映射（冲突昵称），分别精确匹配
    if nickname_mappings:
        matched_ids = set()
        for mapped_name, extra_filters in nickname_mappings:
            if not mapped_name:
                continue
            norm_mapped = _normalize_text(mapped_name)
            for servant in db:
                servant_id = servant.get("collectionNo", id(servant))
                if servant_id in matched_ids:
                    continue
                # 额外过滤器（如职阶限制）
                skip = False
                for attr, val in extra_filters.items():
                    if attr == "className":
                        if servant.get("className", "").lower() != val.lower():
                            skip = True
                            break
                if skip:
                    continue

                norm_en = _normalize_text(servant.get("name", ""))
                norm_cn = _normalize_text(servant.get("aliasCN", ""))
                norm_jp = _normalize_text(servant.get("originalName", ""))

                if norm_mapped in (norm_en, norm_cn, norm_jp):
                    exact_matches.append(servant)
                    matched_ids.add(servant_id)

        if exact_matches:
            return exact_matches

    # 无昵称映射或昵称映射未命中时，走子串模糊匹配
    for servant in db:
        en_name = servant.get("name", "").lower()
        cn_name = servant.get("aliasCN", "").lower()
        jp_name = servant.get("originalName", "").lower()
        norm_en = _normalize_text(en_name)
        norm_cn = _normalize_text(cn_name)
        norm_jp = _normalize_text(jp_name)

        # 阶段 2: 子串模糊匹配
        if len(normalized_query) >= 2:
            if normalized_query in norm_en or normalized_query in norm_cn or normalized_query in norm_jp:
                substring_matches.append(servant)
                continue

        # 阶段 3: 反向子串匹配
        if (
            (norm_en and norm_en in normalized_query)
            or (norm_cn and norm_cn in normalized_query)
            or (norm_jp and norm_jp in normalized_query)
        ):
            reverse_matches.append(servant)

    if substring_matches:
        return substring_matches
    return reverse_matches


@register_skill
class LookupServant(QuerySkill):
    name = "lookup_servant"
    description = "按名称查询单个从者（支持中英日名和昵称）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """按名称查找所有匹配的从者候选。"""
        query_name = params.get("name")
        if query_name is None or not isinstance(query_name, str) or not query_name.strip():
            return list(db)

        return find_servant_candidates(db, query_name)
