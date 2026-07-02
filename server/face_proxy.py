"""
Laplace — 从者头像代理路由

开发环境下替代 Nginx 的 /faces/ 静态服务 + 回源代理功能。
生产环境由 Nginx try_files + @atlas_fallback 处理，此路由作为开发兜底。

行为：
1. 本地文件存在 → 直接返回（FileResponse）
2. 本地不存在 → 代理回源 Atlas CDN → 缓存到本地 → 返回
3. 若回源 404 → 尝试灵基降级（如 f_XXX3.png 降为 f_XXX1.png）
"""

import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

router = APIRouter()

# ── 常量 ──
FACES_DIR = Path(__file__).parent / "data" / "faces"
ATLAS_FACE_BASE = "https://static.atlasacademy.io/JP/Faces"
PROXY_TIMEOUT = 15.0  # 回源超时（秒）

# 头像文件名模式: f_{servantId}{ascension}.png，最后一位为灵基编号(1-4)
_FACE_PATTERN = re.compile(r"^(f_\d+?)([1-4])\.png$")


def _ascension_fallback_candidates(filename: str) -> list[str]:
    """生成灵基降级候选文件名列表。

    新从者的高灵基头像可能尚未上传至 Atlas CDN，
    按 降序 尝试低灵基作为 fallback（跳过自身）。
    例如：f_10022003.png → [f_10022002.png, f_10022001.png]
    """
    m = _FACE_PATTERN.match(filename)
    if not m:
        return []
    base, asc_str = m.group(1), m.group(2)
    current_asc = int(asc_str)
    candidates = []
    for asc in range(current_asc - 1, 0, -1):
        candidates.append(f"{base}{asc}.png")
    return candidates


async def _fetch_from_atlas(client: httpx.AsyncClient, filename: str) -> httpx.Response | None:
    """从 Atlas CDN 获取头像，返回成功响应或 None。"""
    url = f"{ATLAS_FACE_BASE}/{filename}"
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            return resp
    except (httpx.TimeoutException, httpx.ConnectError):
        pass
    return None


@router.get("/faces/{filename}")
async def serve_face(filename: str) -> Response:
    """从者头像服务：本地优先，未命中回源 Atlas CDN（支持灵基降级）。"""
    # 安全校验：防止路径遍历
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # 仅允许 .png 文件
    if not filename.endswith(".png"):
        raise HTTPException(status_code=400, detail="Only PNG files supported")

    local_path = FACES_DIR / filename

    # 1. 本地文件存在 → 直接返回
    if local_path.exists():
        return FileResponse(
            local_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=2592000, immutable"},
        )

    # 2. 本地不存在 → 代理回源（带灵基降级）
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            # 优先尝试原始文件名
            resp = await _fetch_from_atlas(client, filename)

            # 原始 404 → 尝试灵基降级候选
            if resp is None:
                for candidate in _ascension_fallback_candidates(filename):
                    # 先检查本地是否有降级文件
                    candidate_local = FACES_DIR / candidate
                    if candidate_local.exists():
                        return FileResponse(
                            candidate_local,
                            media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"},
                        )
                    resp = await _fetch_from_atlas(client, candidate)
                    if resp is not None:
                        break
    except (httpx.TimeoutException, httpx.ConnectError):
        raise HTTPException(status_code=504, detail="Atlas CDN timeout")

    if resp is None:
        raise HTTPException(status_code=404, detail="Face not found")

    # 3. 缓存到本地（使用原始请求的文件名，下次直接命中）
    try:
        local_path.write_bytes(resp.content)
    except OSError:
        pass  # 写入失败不影响本次请求

    # 4. 返回图片（降级命中用较短缓存，待 Atlas 上传后可刷新）
    cache_control = "public, max-age=2592000, immutable"
    if resp.url and str(resp.url).split("/")[-1] != filename:
        cache_control = "public, max-age=86400"  # 降级命中: 1天缓存
    return Response(
        content=resp.content,
        media_type="image/png",
        headers={"Cache-Control": cache_control},
    )
