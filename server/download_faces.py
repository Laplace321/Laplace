"""
Laplace — 从者头像批量下载脚本

从 servants_db.json 中提取所有从者头像 URL，
批量下载到 server/data/faces/ 目录。
支持增量模式（跳过已存在文件）和并发控制。

用法：
    python -m server.download_faces
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests", file=sys.stderr)
    sys.exit(1)

# ── 常量 ──
DATA_DIR = Path(__file__).parent / "data"
FACES_DIR = DATA_DIR / "faces"
DB_PATH = DATA_DIR / "servants_db.json"
CE_DB_PATH = DATA_DIR / "craft_essences_db.json"
ATLAS_FACE_BASE = "https://static.atlasacademy.io/JP/Faces"

# 并发下载数
MAX_WORKERS = 10
# 单张下载超时（秒）
TIMEOUT = 30


def _extract_face_urls_from_db(db_path: Path) -> list[dict[str, str]]:
    """从数据库文件提取所有头像下载信息。

    支持从者数据库和礼装数据库（共用 _faceUrlSource 字段）。

    Returns:
        [{"filename": "f_1001003.png", "url": "https://..."}]
    """
    if not db_path.exists():
        return []

    with open(db_path, encoding="utf-8") as f:
        db = json.load(f)

    results = []
    for entry in db:
        source_url = entry.get("_faceUrlSource", "")
        if not source_url:
            continue
        # 从 URL 提取文件名
        parsed = urlparse(source_url)
        filename = Path(parsed.path).name
        if filename:
            results.append({"filename": filename, "url": source_url})

    return results


def _extract_face_urls(db_path: Path) -> list[dict[str, str]]:
    """从从者和礼装数据库提取所有头像下载信息。

    Returns:
        [{"filename": "f_1001003.png", "url": "https://..."}]
    """
    results = _extract_face_urls_from_db(db_path)
    # 同时从礼装数据库提取
    ce_results = _extract_face_urls_from_db(CE_DB_PATH)
    if ce_results:
        # 去重（以 filename 为 key）
        existing = {r["filename"] for r in results}
        for item in ce_results:
            if item["filename"] not in existing:
                results.append(item)
                existing.add(item["filename"])
    return results


def _download_one(item: dict[str, str], faces_dir: Path) -> tuple[str, bool, str]:
    """下载单张图片。

    Returns:
        (filename, success, error_msg)
    """
    filename = item["filename"]
    url = item["url"]
    target = faces_dir / filename

    # 增量模式：已存在则跳过
    if target.exists():
        return (filename, True, "skipped")

    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        target.write_bytes(resp.content)
        return (filename, True, "")
    except Exception as e:
        return (filename, False, str(e))


def download_all_faces() -> dict[str, int]:
    """批量下载所有从者头像。

    Returns:
        {"total": N, "downloaded": N, "skipped": N, "failed": N}
    """
    FACES_DIR.mkdir(parents=True, exist_ok=True)

    items = _extract_face_urls(DB_PATH)
    if not items:
        print("[download_faces] 无头像需要下载")
        return {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}

    total = len(items)
    downloaded = 0
    skipped = 0
    failed = 0
    failed_list: list[str] = []

    print(f"[download_faces] 开始下载 {total} 张头像到 {FACES_DIR}")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_download_one, item, FACES_DIR): item for item in items}
        for future in as_completed(futures):
            filename, success, msg = future.result()
            if success:
                if msg == "skipped":
                    skipped += 1
                else:
                    downloaded += 1
            else:
                failed += 1
                failed_list.append(f"  {filename}: {msg}")

    print(f"[download_faces] 完成: 下载 {downloaded}, 跳过 {skipped}, 失败 {failed}")
    if failed_list:
        print("[download_faces] 失败列表:", file=sys.stderr)
        for line in failed_list:
            print(line, file=sys.stderr)

    return {
        "total": total,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
    }


if __name__ == "__main__":
    result = download_all_faces()
    if result["failed"] > 0:
        sys.exit(1)
