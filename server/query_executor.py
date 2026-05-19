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
CE_DATA_PATH = Path(__file__).parent / "data" / "craft_essences_db.json"
FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "servants_fixture.json"
CE_FIXTURE_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "ce_fixture.json"
NICKNAMES_PATH = Path(__file__).parent / "config" / "nicknames.json"
CE_NICKNAMES_PATH = Path(__file__).parent / "config" / "ce_nicknames.json"

# 全局缓存
_servants_db: list[dict] | None = None
_ce_db: list[dict] | None = None

_nicknames_cache = CachedConfig(NICKNAMES_PATH)


def _normalize_text(value: str) -> str:
    """Normalize names for nickname and substring matching."""
    text = value.strip().lower()
    text = re.sub(r"[\s·•・\-.()（）〔〕\[\]「」『』_]+", "", text)
    return text


_ce_nicknames_cache = CachedConfig(CE_NICKNAMES_PATH)


def load_nicknames() -> dict[str, str]:
    """加载从者昵称映射（支持热更新）。"""
    return _nicknames_cache.get()


def load_ce_nicknames() -> dict[str, str]:
    """加载礼装昵称映射（支持热更新）。

    Returns:
        {"万花筒": "Kaleidoscope", "黑杯": "Heaven's Feel", ...}
    """
    return _ce_nicknames_cache.get()


def load_database() -> list[dict]:
    """加载从者数据库（带缓存）。

    优先加载真实数据（server/data/servants_db.json），
    若不存在则 fallback 到测试 fixture 数据（tests/fixtures/servants_fixture.json），
    确保 CI 环境下测试可正常运行。
    """
    global _servants_db
    if _servants_db is None:
        data_path = DATA_PATH if DATA_PATH.exists() else FIXTURE_PATH
        with open(data_path, encoding="utf-8") as f:
            _servants_db = json.load(f)
        has_effects = sum(1 for s in _servants_db if s.get("skillEffects"))
        label = "fixture" if data_path == FIXTURE_PATH else "full"
        print(f"📦 从者数据库已加载: {len(_servants_db)} 条, {has_effects} 个有效果数据 ({label})")
    return _servants_db


def load_ce_database() -> list[dict]:
    """加载概念礼装数据库（带缓存）。

    优先加载真实数据（server/data/craft_essences_db.json），
    若不存在则 fallback 到测试 fixture 数据（tests/fixtures/ce_fixture.json），
    确保 CI 环境下测试可正常运行。
    """
    global _ce_db
    if _ce_db is None:
        if CE_DATA_PATH.exists():
            data_path = CE_DATA_PATH
        elif CE_FIXTURE_PATH.exists():
            data_path = CE_FIXTURE_PATH
        else:
            print("⚠️  礼装数据库不存在，CE 查询将返回空结果")
            _ce_db = []
            return _ce_db
        with open(data_path, encoding="utf-8") as f:
            _ce_db = json.load(f)
        has_effects = sum(1 for ce in _ce_db if ce.get("effectsLimitBreak"))
        label = "fixture" if data_path == CE_FIXTURE_PATH else "full"
        print(f"🎴 礼装数据库已加载: {len(_ce_db)} 条, {has_effects} 个有效果数据 ({label})")
    return _ce_db


def _match_target_type(query_type: str, data_type: str) -> bool:
    """匹配目标类型。

    - party 查询匹配 party + ptOne + partyOther（partyOther 有额外复合判定，在上层处理）
    - self 查询仅匹配 self
    - partyOther 查询仅匹配 partyOther
    """
    if query_type == data_type:
        return True
    if query_type == "party" and data_type in ("ptOne", "partyOther"):
        return True
    return False


def _match_effect(
    servant: dict,
    effect_name: str,
    target_type: str | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> bool:
    """检查从者的技能是否拥有特定效果（支持目标类型和数值条件筛选）。

    Args:
        servant: 从者数据
        effect_name: 效果名（如 "invincible"）
        target_type: 目标类型筛选（如 "party"/"self"/"partyOther"），None 表示不限
        min_value: 效果最小数值（千分比‰），None 表示不限
        max_value: 效果最大数值（千分比‰），None 表示不限

    partyOther 复合判定：
        当 targetType="party" 且从者有 partyOther 类效果时，需额外验证自身也能
        获得足够的同效果值（self + party + ptOne >= min_value），否则 partyOther
        部分的值不计入全队总量。
    """
    # 快速路径：先检查 skillEffects 集合
    servant_effects = servant.get("skillEffects", [])
    if effect_name not in servant_effects:
        return False

    # 如果有任何精细条件，遍历 skillDetails 做三维过滤
    if target_type is not None or min_value is not None or max_value is not None:
        # 分桶累加各类 targetType 的值
        self_value = 0
        party_value = 0
        pt_one_value = 0
        party_other_value = 0

        for skill in servant.get("skillDetails", []):
            for eff in skill.get("effects", []):
                if eff.get("type") != effect_name:
                    continue
                eff_target = eff.get("targetType", "")
                val = eff.get("valueMax", 0)
                if eff_target == "self":
                    self_value += val
                elif eff_target == "party":
                    party_value += val
                elif eff_target == "ptOne":
                    pt_one_value += val
                elif eff_target == "partyOther":
                    party_other_value += val

        # 根据查询 targetType 决定累加策略
        if target_type == "party":
            # "全队"查询：party + ptOne + partyOther（partyOther 有附加条件）
            team_value = party_value + pt_one_value + party_other_value
            if team_value == 0:
                return False
            # partyOther 复合判定：自身也必须满足阈值
            if party_other_value > 0 and min_value is not None:
                self_total = self_value + party_value + pt_one_value
                if self_total < min_value:
                    return False
            total_value = team_value
        elif target_type == "self":
            # 自身查询：self + party（全队含自己）+ ptOne（可指定自身）
            total_value = self_value + party_value + pt_one_value
        elif target_type == "partyOther":
            total_value = party_other_value
        elif target_type == "ptOne":
            total_value = pt_one_value + party_value
        else:
            # 不限目标：全部累加
            total_value = self_value + party_value + pt_one_value + party_other_value

        if total_value == 0:
            return False
        if min_value is not None and total_value < min_value:
            return False
        if max_value is not None and total_value > max_value:
            return False
        return True

    return True


def _match_np_effect(
    servant: dict,
    effect_name: str,
    target_type: str | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
) -> bool:
    """检查从者的宝具是否拥有特定效果（OC1 Lv1 数值，支持目标类型和数值条件筛选）。

    Args:
        servant: 从者数据
        effect_name: 效果名（如 "upBuster"）
        target_type: 目标类型筛选（如 "party"/"self"/"partyOther"），None 表示不限
        min_value: 效果最小数值（千分比‰），None 表示不限
        max_value: 效果最大数值（千分比‰），None 表示不限

    partyOther 复合判定逻辑与 _match_effect 保持一致。
    """
    # 快速路径：先检查 npEffects 集合
    if effect_name not in servant.get("npEffects", []):
        return False

    # 如果有精细条件，遍历 npDetails 做三维过滤
    if target_type is not None or min_value is not None or max_value is not None:
        # 分桶累加各类 targetType 的值
        self_value = 0
        party_value = 0
        pt_one_value = 0
        party_other_value = 0

        for np_detail in servant.get("npDetails", []):
            for eff in np_detail.get("effects", []):
                if eff.get("type") != effect_name:
                    continue
                eff_target = eff.get("targetType", "")
                val = eff.get("valueLv1", 0)
                if eff_target == "self":
                    self_value += val
                elif eff_target == "party":
                    party_value += val
                elif eff_target == "ptOne":
                    pt_one_value += val
                elif eff_target == "partyOther":
                    party_other_value += val

        # 根据查询 targetType 决定累加策略
        if target_type == "party":
            team_value = party_value + pt_one_value + party_other_value
            if team_value == 0:
                return False
            if party_other_value > 0 and min_value is not None:
                self_total = self_value + party_value + pt_one_value
                if self_total < min_value:
                    return False
            total_value = team_value
        elif target_type == "self":
            total_value = self_value + party_value + pt_one_value
        elif target_type == "partyOther":
            total_value = party_other_value
        elif target_type == "ptOne":
            total_value = pt_one_value + party_value
        else:
            total_value = self_value + party_value + pt_one_value + party_other_value

        if total_value == 0:
            return False
        if min_value is not None and total_value < min_value:
            return False
        if max_value is not None and total_value > max_value:
            return False
        return True

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
