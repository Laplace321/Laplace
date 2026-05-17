"""
Laplace — 从者头像代理路由

开发环境下替代 Nginx 的 /faces/ 静态服务 + 回源代理功能。
生产环境由 Nginx try_files + @atlas_fallback 处理，此路由作为开发兜底。

行为：
1. 本地文件存在 → 直接返回（FileResponse）
2. 本地不存在 → 代理回源 Atlas CDN → 缓存到本地 → 返回
"""

from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

router = APIRouter()

# ── 常量 ──
FACES_DIR = Path(__file__).parent / "data" / "faces"
ATLAS_FACE_BASE = "https://static.atlasacademy.io/JP/Faces"
PROXY_TIMEOUT = 15.0  # 回源超时（秒）


@router.get("/faces/{filename}")
async def serve_face(filename: str) -> Response:
    """从者头像服务：本地优先，未命中回源 Atlas CDN。"""
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

    # 2. 本地不存在 → 代理回源
    source_url = f"{ATLAS_FACE_BASE}/{filename}"
    try:
        async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
            resp = await client.get(source_url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Face not found")
        raise HTTPException(status_code=502, detail="Atlas CDN error")
    except (httpx.TimeoutException, httpx.ConnectError):
        raise HTTPException(status_code=504, detail="Atlas CDN timeout")

    # 3. 缓存到本地（容错：目录不存在则创建）
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    try:
        local_path.write_bytes(resp.content)
    except OSError:
        pass  # 写入失败不影响本次请求

    # 4. 返回图片
    return Response(
        content=resp.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=2592000, immutable"},
    )
