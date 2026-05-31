#!/usr/bin/env python3
"""
Mooncell Wiki 从者昵称同步脚本

从 fgo.wiki（Mooncell）批量抓取所有从者的社区昵称数据，
通过 collectionNo 映射到 Atlas 数据库，生成自动化昵称表。

定位与 sync_chaldea.py 一致 — build-time 数据同步工具，非 runtime 依赖。
输出: server/config/mooncell_nicknames.json

用法:
    python extractor/sync_mooncell_nicknames.py
    python extractor/sync_mooncell_nicknames.py --dry-run   # 仅预览，不写入文件
"""

import json
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests", file=sys.stderr)
    sys.exit(1)

# ── 常量 ──────────────────────────────────────────────────────

MOONCELL_API = "https://fgo.wiki/api.php"
CATEGORY_NAME = "Category:从者"
BATCH_SIZE = 50
REQUEST_INTERVAL = 0.5  # 秒，请求间隔（礼貌爬虫）
USER_AGENT = "LaplaceBot/1.0 (FGO Servant Nickname Sync; +https://github.com/Laplace321/Laplace)"

OUTPUT_PATH = Path(__file__).parent.parent / "server" / "config" / "mooncell_nicknames.json"
SERVANTS_DB_PATH = Path(__file__).parent.parent / "server" / "data" / "servants_db.json"

# 昵称字段正则
RE_NICKNAME = re.compile(r"\|昵称=([^\n|]*)")
RE_COLLECTION_NO = re.compile(r"\|序号=(\d+)")
# 括号补充说明提取（如 "拉二（拉美西斯二世）" → 额外提取 "拉二"）
RE_PAREN_SUFFIX = re.compile(r"^(.+?)[（(].+[）)]$")


# ── MediaWiki API 交互 ──────────────────────────────────────


def _create_session() -> requests.Session:
    """创建带 User-Agent 的请求会话。"""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def fetch_servant_titles(session: requests.Session) -> list[str]:
    """从 Mooncell 获取「从者」分类下所有页面标题。"""
    titles = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": CATEGORY_NAME,
        "cmlimit": 500,
        "cmtype": "page",
        "format": "json",
    }

    while True:
        response = session.get(MOONCELL_API, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        members = data.get("query", {}).get("categorymembers", [])
        titles.extend(member["title"] for member in members)

        # 分页处理
        continuation = data.get("continue")
        if not continuation:
            break
        params.update(continuation)
        time.sleep(REQUEST_INTERVAL)

    return titles


def fetch_page_contents(session: requests.Session, titles: list[str]) -> dict[str, str]:
    """批量获取页面 wikitext 源码。返回 {title: wikitext}。"""
    results = {}

    for batch_start in range(0, len(titles), BATCH_SIZE):
        batch = titles[batch_start : batch_start + BATCH_SIZE]
        titles_param = "|".join(batch)

        params = {
            "action": "query",
            "titles": titles_param,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
        }

        response = session.get(MOONCELL_API, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            title = page.get("title", "")
            revisions = page.get("revisions", [])
            if revisions:
                content = revisions[0].get("slots", {}).get("main", {}).get("*", "") or revisions[0].get("*", "")
                results[title] = content

        progress = min(batch_start + BATCH_SIZE, len(titles))
        print(f"   获取页面源码: {progress}/{len(titles)}")
        time.sleep(REQUEST_INTERVAL)

    return results


# ── 解析与映射 ──────────────────────────────────────────────


def parse_nicknames_from_wikitext(wikitext: str) -> tuple[list[str], int | None]:
    """从 wikitext 中提取昵称列表和序号。

    Returns:
        (昵称列表, collectionNo 或 None)
    """
    # 提取序号
    collection_match = RE_COLLECTION_NO.search(wikitext)
    collection_no = int(collection_match.group(1)) if collection_match else None

    # 提取昵称
    nickname_match = RE_NICKNAME.search(wikitext)
    if not nickname_match:
        return [], collection_no

    raw_nicknames = nickname_match.group(1).strip()
    if not raw_nicknames:
        return [], collection_no

    # 按逗号分割（支持中英文逗号）
    nicknames = [nick.strip() for nick in re.split(r"[,，]", raw_nicknames) if nick.strip()]

    # 括号补充说明处理：如果昵称包含括号补充，额外提取括号前的部分
    expanded = []
    for nick in nicknames:
        expanded.append(nick)
        paren_match = RE_PAREN_SUFFIX.match(nick)
        if paren_match:
            short_form = paren_match.group(1).strip()
            if short_form and short_form not in expanded:
                expanded.append(short_form)

    return expanded, collection_no


def build_servant_lookup(db_path: Path) -> dict[int, dict]:
    """从 servants_db.json 构建 collectionNo → 从者信息的查找表。"""
    if not db_path.exists():
        print(f"⚠️  servants_db.json 不存在: {db_path}", file=sys.stderr)
        print("   请先运行 data_loader.py 生成数据库", file=sys.stderr)
        return {}

    with open(db_path, encoding="utf-8") as f:
        servants = json.load(f)

    lookup = {}
    for servant in servants:
        collection_no = servant.get("collectionNo", 0)
        if collection_no > 0:
            lookup[collection_no] = {
                "name": servant.get("aliasCN") or servant.get("name", ""),
                "className": servant.get("className", "unknown"),
            }
    return lookup


def generate_nickname_mapping(
    page_contents: dict[str, str],
    servant_lookup: dict[int, dict],
) -> tuple[dict, dict]:
    """生成昵称映射表和统计信息。

    Returns:
        (nickname_mapping, stats)
    """
    nickname_mapping: dict[str, list[dict]] = {}  # 临时用 list 收集冲突
    stats = {
        "total_pages": len(page_contents),
        "pages_with_nicknames": 0,
        "pages_without_nicknames": 0,
        "pages_no_collection_no": 0,
        "pages_unmapped": 0,
        "total_nickname_entries": 0,
        "conflicts": [],  # 同一昵称指向多个从者
    }

    for title, wikitext in page_contents.items():
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)

        if not nicknames:
            stats["pages_without_nicknames"] += 1
            continue

        stats["pages_with_nicknames"] += 1

        if collection_no is None:
            stats["pages_no_collection_no"] += 1
            print(f"   ⚠️  页面「{title}」无序号字段，跳过")
            continue

        servant_info = servant_lookup.get(collection_no)
        if not servant_info:
            stats["pages_unmapped"] += 1
            print(f"   ⚠️  页面「{title}」序号 {collection_no} 在 servants_db 中未找到，跳过")
            continue

        servant_name = servant_info["name"]
        class_name = servant_info["className"]

        for nick in nicknames:
            entry = {
                "name": servant_name,
                "className": class_name,
                "_source": "mooncell",
                "_collectionNo": collection_no,
            }

            if nick not in nickname_mapping:
                nickname_mapping[nick] = [entry]
            else:
                # 检查是否真正冲突（不同从者）
                existing = nickname_mapping[nick]
                is_duplicate = any(e["_collectionNo"] == collection_no for e in existing)
                if not is_duplicate:
                    nickname_mapping[nick].append(entry)

            stats["total_nickname_entries"] += 1

    # 转换为最终格式：单映射用 dict，多映射（冲突）用 list
    final_mapping = {}
    for nick, entries in nickname_mapping.items():
        if len(entries) == 1:
            final_mapping[nick] = entries[0]
        else:
            # 冲突：同一昵称指向多个从者，全部保留
            final_mapping[nick] = entries
            conflict_servants = [f"{e['name']}({e['className']})" for e in entries]
            stats["conflicts"].append(
                {
                    "nickname": nick,
                    "servants": conflict_servants,
                }
            )

    return final_mapping, stats


# ── 输出 ────────────────────────────────────────────────────


def write_output(mapping: dict, output_path: Path) -> None:
    """写入 JSON 文件，包含禁止手编的注释头。"""
    output = {
        "_comment": "此文件由 extractor/sync_mooncell_nicknames.py 自动生成，禁止手工编辑。手工昵称请编辑 nicknames.json。",
        **mapping,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"\n✅ 已写入: {output_path}")
    print(f"   文件大小: {output_path.stat().st_size / 1024:.1f} KB")


def print_stats(stats: dict) -> None:
    """打印统计报告。"""
    print("\n" + "=" * 50)
    print("📊 同步统计")
    print("=" * 50)
    print(f"   总页面数:       {stats['total_pages']}")
    print(f"   有昵称:         {stats['pages_with_nicknames']}")
    print(f"   无昵称/为空:    {stats['pages_without_nicknames']}")
    print(f"   无序号字段:     {stats['pages_no_collection_no']}")
    print(f"   序号未匹配:     {stats['pages_unmapped']}")
    print(f"   昵称总条目:     {stats['total_nickname_entries']}")
    print(f"   冲突数:         {len(stats['conflicts'])}")

    if stats["conflicts"]:
        print("\n⚠️  昵称冲突（同一昵称指向多个从者）:")
        for conflict in stats["conflicts"]:
            servants_str = ", ".join(conflict["servants"])
            print(f"   「{conflict['nickname']}」→ {servants_str}")


# ── 主流程 ──────────────────────────────────────────────────


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    print("=" * 50)
    print("🌙 Mooncell 从者昵称同步")
    print("=" * 50)

    # 1. 加载 servants_db 查找表
    print("\n📚 加载 servants_db...")
    servant_lookup = build_servant_lookup(SERVANTS_DB_PATH)
    if not servant_lookup:
        print("❌ 无法加载 servants_db，中止", file=sys.stderr)
        sys.exit(1)
    print(f"   ✅ 加载 {len(servant_lookup)} 个从者")

    # 2. 获取从者页面列表
    print("\n📡 获取 Mooncell 从者页面列表...")
    session = _create_session()
    titles = fetch_servant_titles(session)
    print(f"   ✅ 获取到 {len(titles)} 个从者页面")

    # 3. 批量获取页面源码
    print("\n📥 批量获取页面 wikitext...")
    page_contents = fetch_page_contents(session, titles)
    print(f"   ✅ 获取到 {len(page_contents)} 个页面内容")

    # 4. 解析并生成映射
    print("\n🔧 解析昵称并生成映射...")
    mapping, stats = generate_nickname_mapping(page_contents, servant_lookup)

    # 5. 输出统计
    print_stats(stats)

    # 6. 写入文件
    if dry_run:
        print("\n🔍 [dry-run] 预览前 20 条:")
        for i, (nick, data) in enumerate(mapping.items()):
            if i >= 20:
                print("   ...")
                break
            if isinstance(data, list):
                targets = ", ".join(f"{d['name']}({d['className']})" for d in data)
                print(f"   「{nick}」→ [{targets}] (冲突)")
            else:
                print(f"   「{nick}」→ {data['name']}({data['className']})")
        print("\n💡 去掉 --dry-run 参数以写入文件")
    else:
        write_output(mapping, OUTPUT_PATH)

    print("\n✨ 完成!")


if __name__ == "__main__":
    main()
