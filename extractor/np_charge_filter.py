#!/usr/bin/env python3
"""
Laplace — NP Charge Filter (Legacy Wrapper)

向后兼容的 30% 自充从者筛选器。
已迁移至 server/data_loader.py，此脚本仅保留旧版 JSON 输出格式。

使用方式：
    python3 extractor/np_charge_filter.py

输出：
    demo/data/servants_np_charge.json（旧版格式，向后兼容）
"""

import json
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from server.data_loader import extract_np_charges, fetch_servants
except ImportError as e:
    print(f"❌ 导入失败: {e}", file=sys.stderr)
    print("请确保在项目根目录运行此脚本", file=sys.stderr)
    sys.exit(1)

# === 配置 ===
TARGET_CHARGE_PERCENT = 30  # 目标：30% 自充
OUTPUT_DIR = Path(__file__).parent.parent / "demo" / "data"


def main():
    """从新架构获取数据，输出旧格式 JSON（向后兼容）。"""
    print("=" * 60)
    print("🔮 Laplace — NP Charge Filter (Legacy Wrapper)")
    print(f"   目标: 精确 {TARGET_CHARGE_PERCENT}% 自充从者")
    print("   数据源: server/data_loader.py")
    print("=" * 60)

    # 1. 从新架构拉取数据（复用 data_loader.py）
    print("\n📡 正在从 Atlas Academy API 拉取从者数据...")
    servants = fetch_servants()

    # 2. 筛选 30% 自充从者
    print(f"\n🔍 筛选 {TARGET_CHARGE_PERCENT}% 精确自充从者...")

    all_results = []
    seen_ids = set()

    for svt in servants:
        charges = extract_np_charges(svt)

        # 查找精确匹配 TARGET_CHARGE_PERCENT 的充能
        for charge in charges:
            if charge["chargePercent"] == TARGET_CHARGE_PERCENT:
                # 按从者 ID 去重，保留第一个匹配的技能
                if svt["id"] not in seen_ids:
                    seen_ids.add(svt["id"])
                    all_results.append(
                        {
                            "id": svt["id"],
                            "collectionNo": svt.get("collectionNo", 0),
                            "name": svt.get("name", "Unknown"),
                            "rarity": svt.get("rarity", 0),
                            "className": svt.get("className", "unknown"),
                            "chargePercent": charge["chargePercent"],
                            "skillName": charge["skillName"],
                            "skillNum": charge["skillNum"],
                            "targetType": charge["targetType"],
                            "faceUrl": svt.get("faceUrl", ""),
                        }
                    )
                break  # 找到第一个匹配后跳到下一个从者

    # 按稀有度降序 → collectionNo 升序排列
    all_results.sort(key=lambda x: (-x["rarity"], x["collectionNo"]))

    print(f"   ✅ 找到 {len(all_results)} 个从者")

    # 3. 输出旧版格式（向后兼容）
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / "servants_np_charge.json"

    output_data = {
        "query": {
            "type": "QUERY_CHARGE",
            "value": TARGET_CHARGE_PERCENT,
            "description": f"精确 {TARGET_CHARGE_PERCENT}% 自充从者（技能 Lv.10）",
            "funcType": "gainNp",
            "targetTypes": ["self", "ptAll"],
        },
        "count": len(all_results),
        "servants": all_results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"\n📄 输出文件: {output_path}")
    print(f"   从者数量: {len(all_results)}")

    # 打印前 5 个作为预览
    print("\n📋 预览 (前 5 个):")
    for svt in all_results[:5]:
        print(
            f"   ★{'★' * (svt['rarity'] - 1)} {svt['name']} "
            f"({svt['className']}) — {svt['skillName']} "
            f"[{svt['chargePercent']}%]"
        )

    print("\n✨ 完成!")
    return all_results


if __name__ == "__main__":
    main()
