"""
Laplace — Data Loader & Shared Utilities

预加载从者数据库，提供昵称映射、文本归一化、效果匹配等共享工具函数。
Skill 模块通过导入本模块获取数据和工具。
"""

import json
import re
from pathlib import Path

from server.config_loader import CachedConfig

DATA_PATH = Path(__file__).parent / "data" / "servants_db.json"
NICKNAMES_PATH = Path(__file__).parent / "config" / "nicknames.json"

# 全局缓存
_servants_db: list[dict] | None = None

_nicknames_cache = CachedConfig(NICKNAMES_PATH)


def _normalize_text(value: str) -> str:
    """Normalize names for nickname and substring matching."""
    text = value.strip().lower()
    text = re.sub(r"[\s·•・\-.()（）〔〕\[\]「」『』_]+", "", text)
    return text


def load_nicknames() -> dict[str, str]:
    """加载昵称映射（支持热更新）。"""
    return _nicknames_cache.get()


def load_database() -> list[dict]:
    """加载从者数据库（带缓存）。"""
    global _servants_db
    if _servants_db is None:
        with open(DATA_PATH, encoding="utf-8") as f:
            _servants_db = json.load(f)
        # 统计
        has_effects = sum(1 for s in _servants_db if s.get("skillEffects"))
        print(f"📦 从者数据库已加载: {len(_servants_db)} 条, {has_effects} 个有效果数据")
    return _servants_db


def _match_effect(servant: dict, effect_name: str, target_type: str | None = None) -> bool:
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
