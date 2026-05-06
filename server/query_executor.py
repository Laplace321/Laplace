"""
Laplace — Query Executor

接收 LLM 解析出的 JSON 查询指令，
在预加载的从者数据上执行筛选。
支持效果类型、NP 充能、职阶、稀有度等多条件组合查询。
"""

import json
import re
from pathlib import Path
from typing import Callable
from server.individuality import filter_by_traits

DATA_PATH = Path(__file__).parent / "data" / "servants_db.json"
NICKNAMES_PATH = Path(__file__).parent / "knowledge" / "nicknames.json"

# 全局缓存
_servants_db: list[dict] | None = None
_nicknames: dict[str, str] | None = None


def _normalize_text(value: str) -> str:
    """Normalize names for nickname and substring matching."""
    text = value.strip().lower()
    text = re.sub(r"[\s·•・\-.()（）〔〕\[\]「」『』_]+", "", text)
    return text


def load_nicknames() -> dict[str, str]:
    """加载昵称映射。"""
    global _nicknames
    if _nicknames is None:
        if NICKNAMES_PATH.exists():
            with open(NICKNAMES_PATH, "r", encoding="utf-8") as f:
                _nicknames = json.load(f)
        else:
            _nicknames = {}
    return _nicknames


def load_database() -> list[dict]:
    """加载从者数据库（带缓存）。"""
    global _servants_db
    if _servants_db is None:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            _servants_db = json.load(f)
        # 统计
        has_effects = sum(1 for s in _servants_db if s.get("skillEffects"))
        print(f"📦 从者数据库已加载: {len(_servants_db)} 条, {has_effects} 个有效果数据")
    return _servants_db


def execute_query(conditions: dict) -> list[dict]:
    """
    根据条件筛选从者。

    Args:
        conditions: LLM 解析出的查询条件
            - npCharge: {"op": "eq"|"gte"|"lte"|"gt", "value": int} | None
            - rarity: {"op": "eq"|"gte"|"lte"|"gt", "value": int} | None
            - className: str | None
            - name: str | None  (单从者查询，向后兼容)
            - names: list[str] | None  (多从者对比，新增)
            - skillEffect: str | None  (单效果筛选)
            - skillEffects: list[str] | None  (多效果 AND 筛选)
            - targetType: str | None  ("self" | "party" | "enemy")
            - traits: list[int] | None  (特性 ID 筛选)
            - excludeTraits: list[int] | None  (排斥特性 ID 筛选)
            - gender: str | None  (male, female, unknown)
            - attribute: str | None  (earth, sky, human, star, beast)
            - cards: dict | None  (如 {"buster": 3})
            - npCard: str | None  (buster, arts, quick)
            - npTarget: str | None  (one, all, support)

    Returns:
        匹配的从者列表
    """
    db = load_database()
    
    # 检查是否为多从者对比查询
    names = conditions.get("names")
    if names and isinstance(names, list) and len(names) > 0:
        # 多从者对比：分别查询每个从者，合并结果
        all_results = []
        for name in names:
            # 为每个名称创建单独的查询条件
            single_conditions = {k: v for k, v in conditions.items() if k != "names"}
            single_conditions["name"] = name
            
            for servant in db:
                if _match_servant(servant, single_conditions):
                    all_results.append(servant)
                    break  # 找到匹配的第一个从者即可
        
        # 去重（按 ID）
        seen_ids = set()
        unique_results = []
        for svt in all_results:
            if svt["id"] not in seen_ids:
                seen_ids.add(svt["id"])
                unique_results.append(svt)
        
        # 按稀有度降序 → collectionNo 升序排序
        unique_results.sort(key=lambda x: (-x["rarity"], x["collectionNo"]))
        return unique_results
    
    # 单从者查询或多条件筛选（原有逻辑）
    results = []
    for servant in db:
        if not _match_servant(servant, conditions):
            continue
        results.append(servant)

    # 按稀有度降序 → collectionNo 升序排序
    results.sort(key=lambda x: (-x["rarity"], x["collectionNo"]))
    return results


# ============================================================
# Filter Registry 机制
# ============================================================

FILTER_REGISTRY: dict[str, Callable[[dict, dict], bool]] = {}


def register_filter(*field_names: str):
    """装饰器：将过滤函数注册到对应的 conditions 字段名。

    同一个函数可注册多个字段（如 traits + excludeTraits），
    _match_servant 会通过 seen_filters 去重，确保只执行一次。
    """
    def decorator(fn: Callable[[dict, dict], bool]):
        for name in field_names:
            FILTER_REGISTRY[name] = fn
        return fn
    return decorator


# ============================================================
# 过滤器实现（按复杂度从低到高排列）
# ============================================================

@register_filter("className")
def _filter_class(servant: dict, conditions: dict) -> bool:
    """职阶精确匹配。"""
    class_name = conditions.get("className")
    if class_name is None:
        return True
    return servant.get("className", "").lower() == class_name.lower()


@register_filter("gender")
def _filter_gender(servant: dict, conditions: dict) -> bool:
    """性别筛选。"""
    gender = conditions.get("gender")
    if gender is None:
        return True
    return servant.get("gender", "") == gender


@register_filter("attribute")
def _filter_attribute(servant: dict, conditions: dict) -> bool:
    """阵营筛选。"""
    attribute = conditions.get("attribute")
    if attribute is None:
        return True
    return servant.get("attribute", "") == attribute


@register_filter("rarity")
def _filter_rarity(servant: dict, conditions: dict) -> bool:
    """稀有度比较。"""
    rarity_cond = conditions.get("rarity")
    if rarity_cond is None:
        return True
    rarity = servant.get("rarity", 0)
    op = rarity_cond.get("op", "eq")
    value = rarity_cond.get("value", 0)
    return _compare(rarity, op, value)


@register_filter("npCharge")
def _filter_np_charge(servant: dict, conditions: dict) -> bool:
    """NP 充能条件（含 op 判断 + 精确值匹配）。"""
    np_cond = conditions.get("npCharge")
    if np_cond is None:
        return True
    if not servant.get("hasNpCharge", False):
        return False
    charge = servant.get("totalSelfCharge", 0)
    op = np_cond.get("op", "eq")
    value = np_cond.get("value", 0)
    if op == "eq":
        return any(
            c["chargePercent"] == value
            for c in servant.get("npCharges", [])
        )
    elif op == "gte":
        return charge >= value
    elif op == "gt":
        return charge > value
    elif op == "lte":
        return charge <= value
    return True


@register_filter("skillEffect")
def _filter_skill_effect(servant: dict, conditions: dict) -> bool:
    """单效果筛选。"""
    skill_effect = conditions.get("skillEffect")
    if skill_effect is None:
        return True
    target_type = conditions.get("targetType")
    return _match_effect(servant, skill_effect, target_type)


@register_filter("skillEffects")
def _filter_skill_effects(servant: dict, conditions: dict) -> bool:
    """多效果组合筛选（AND/OR）。"""
    skill_effects = conditions.get("skillEffects")
    if skill_effects is None or not isinstance(skill_effects, list):
        return True
    target_type = conditions.get("targetType")
    op = conditions.get("skillEffectsOp", "and").lower()

    if op == "or":
        return any(_match_effect(servant, eff, target_type) for eff in skill_effects)
    else:
        return all(_match_effect(servant, eff, target_type) for eff in skill_effects)


@register_filter("traits", "excludeTraits")
def _filter_traits(servant: dict, conditions: dict) -> bool:
    """特性筛选（委托 filter_by_traits）。"""
    traits = conditions.get("traits")
    exclude_traits = conditions.get("excludeTraits")
    if not traits and not exclude_traits:
        return True
    servant_traits = servant.get("traits", [])
    return filter_by_traits(servant_traits, traits, exclude_traits)


@register_filter("cards", "npCard", "npTarget")
def _filter_cards(servant: dict, conditions: dict) -> bool:
    """配卡 + 宝具颜色 + 宝具目标筛选。"""
    # 配卡
    cards = conditions.get("cards")
    if cards is not None and isinstance(cards, dict):
        servant_cards = servant.get("cards", {})
        for card_type, count in cards.items():
            if servant_cards.get(card_type, 0) < count:
                return False

    # 宝具颜色
    np_card = conditions.get("npCard")
    if np_card is not None:
        if servant.get("npCard", "") != np_card:
            return False

    # 宝具目标
    np_target = conditions.get("npTarget")
    if np_target is not None:
        if servant.get("npTarget", "") != np_target:
            return False

    return True


@register_filter("name")
def _filter_name(servant: dict, conditions: dict) -> bool:
    """名称分级匹配：精确 → 子串模糊 → 昵称映射。"""
    name = conditions.get("name")
    if name is None or not isinstance(name, str) or not name.strip():
        return True

    query_name = name.strip()
    normalized_query_name = _normalize_text(query_name)

    # 尝试昵称转换
    nicknames = load_nicknames()
    mapped_data = None
    for nick, data in nicknames.items():
        if _normalize_text(nick) == normalized_query_name:
            mapped_data = data
            break

    # 处理昵称映射
    mapped_name = None
    extra_filters = {}
    if isinstance(mapped_data, str):
        mapped_name = mapped_data.lower()
    elif isinstance(mapped_data, dict):
        mapped_name = mapped_data.get("name", "").lower()
        for k, v in mapped_data.items():
            if k != "name":
                extra_filters[k] = v

    # 检查额外过滤器（如职阶）
    for attr, val in extra_filters.items():
        if attr == "className":
            if servant.get("className", "").lower() != val.lower():
                return False

    en_name = servant.get("name", "").lower()
    cn_name = servant.get("aliasCN", "").lower()
    jp_name = servant.get("originalName", "").lower()
    normalized_en_name = _normalize_text(en_name)
    normalized_cn_name = _normalize_text(cn_name)
    normalized_jp_name = _normalize_text(jp_name)

    # 分级匹配策略
    name_matched = False

    # 阶段 1: 精确匹配（有映射名时优先检查映射）
    if mapped_name:
        normalized_mapped_name = _normalize_text(mapped_name)
        if (normalized_mapped_name == normalized_en_name or
            normalized_mapped_name == normalized_cn_name or
            normalized_mapped_name == normalized_jp_name):
            name_matched = True

    # 阶段 2: 子串模糊匹配（"武尊" in "大和武尊"）
    if not name_matched and len(normalized_query_name) >= 2:
        if (normalized_query_name in normalized_en_name or
            normalized_query_name in normalized_cn_name or
            normalized_query_name in normalized_jp_name):
            name_matched = True

    # 阶段 3: 反向子串匹配
    if not name_matched:
        if (normalized_en_name and normalized_en_name in normalized_query_name) or \
           (normalized_cn_name and normalized_cn_name in normalized_query_name) or \
           (normalized_jp_name and normalized_jp_name in normalized_query_name):
            name_matched = True

    return name_matched


# ============================================================
# 核心匹配逻辑
# ============================================================

def _match_servant(servant: dict, conditions: dict) -> bool:
    """检查单个从者是否匹配所有已注册的过滤条件。"""
    seen_filters: set[int] = set()
    for field, filter_fn in FILTER_REGISTRY.items():
        fn_id = id(filter_fn)
        if fn_id in seen_filters:
            continue  # 同一函数注册了多个字段，只执行一次
        if conditions.get(field) is not None:
            seen_filters.add(fn_id)
            if not filter_fn(servant, conditions):
                return False
    return True


def _match_effect(
    servant: dict, effect_name: str, target_type: str | None = None
) -> bool:
    """
    检查从者是否拥有特定效果。

    Args:
        servant: 从者数据
        effect_name: 效果名（如 "invincible"）
        target_type: 目标类型筛选（如 "party"），None 表示不限
    """
    # 快速路径：先检查 skillEffects 集合
    servant_effects = servant.get("skillEffects", [])
    if effect_name not in servant_effects:
        return False

    # 如果需要按目标类型筛选，检查详细数据
    if target_type is not None:
        for skill in servant.get("skillDetails", []):
            for eff in skill.get("effects", []):
                if eff.get("type") == effect_name:
                    if eff.get("targetType") == target_type:
                        return True
        return False

    return True


def _compare(actual: int, op: str, expected: int) -> bool:
    """通用比较操作。"""
    if op == "eq":
        return actual == expected
    elif op == "gte":
        return actual >= expected
    elif op == "gt":
        return actual > expected
    elif op == "lte":
        return actual <= expected
    elif op == "lt":
        return actual < expected
    return False
