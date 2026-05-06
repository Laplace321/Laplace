"""
Laplace — Individuality (特性) 检查器

实现 FGO 特性匹配逻辑。对应 Chaldea 源码:
chaldea/lib/models/gamedata/individuality.dart
"""


def divide_unsigned_and_signed(base_array: list[int]) -> tuple[list[int], list[int]]:
    """
    分离正特性和负特性。
    FGO 中负特性表示排斥（必须不具备该特性）。
    """
    unsigned = []
    signed = []
    for x in base_array:
        if x < 1:
            signed.append(-x)
        else:
            unsigned.append(x)
    return unsigned, signed


def is_partial_match(self_traits: list[int], targets: list[int]) -> bool:
    """如果 targets 中有任何一个特性在 self_traits 中，则返回 True。"""
    self_set = set(self_traits)
    return any(t in self_set for t in targets)


def check_signed_individualities(self_traits: list[int], signed_targets: list[int]) -> bool:
    """
    校验从者是否满足带符号的特性条件。

    Args:
        self_traits: 从者自身拥有的特性 ID 列表
        signed_targets: 查询条件的特性 ID 列表（正数为必须拥有，负数为不能拥有）
    """
    if not signed_targets:
        return True

    if not self_traits:
        return False

    unsigned_array, signed_array = divide_unsigned_and_signed(signed_targets)

    # 必须至少满足一个正特性（如果有正特性条件）
    v11 = True
    if unsigned_array:
        v11 = is_partial_match(self_traits, unsigned_array)

    # 必须不能满足任何负特性（如果有负特性条件）
    v13 = True
    if signed_array:
        v13 = not is_partial_match(self_traits, signed_array)

    return v11 and v13


def filter_by_traits(
    servant_traits: list[int], required_traits: list[int], exclude_traits: list[int] | None = None
) -> bool:
    """
    用于查询的高级接口：检查是否满足所有必须特性，且不包含排斥特性。
    这是 AND 逻辑（通常查询时希望拥有 A 且拥有 B）。
    """
    self_set = set(servant_traits)

    # 检查必须拥有的特性（AND 逻辑：每一个都必须有）
    if required_traits:
        for t in required_traits:
            if t not in self_set:
                return False

    # 检查不能拥有的特性
    if exclude_traits:
        for t in exclude_traits:
            if t in self_set:
                return False

    return True
