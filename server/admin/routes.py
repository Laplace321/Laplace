"""
Laplace — Admin 路由模块

环境变量管理 + 配置文件 CRUD + Docker restart API。
所有路由通过 require_admin 依赖保护。
"""

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.admin.auth import require_admin

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])

# ── 路径常量 ──
_ENV_PATH = Path("/app/.env")  # Docker 内 volume 挂载路径
_LOCAL_ENV_PATH = Path(__file__).parent.parent.parent / ".env"  # 本地开发路径
_CONFIG_DIR = Path(__file__).parent.parent / "config"


# 需要从 os.environ 展示的关键环境变量（过滤掉系统级噪音）
_DISPLAY_ENV_KEYS = [
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ADMIN_PASSWORD",
    "CONTAINER_NAME",
    "REFRESH_DATA_ON_START",
    "UVICORN_WORKERS",
    "CHALDEA_SRC_PATH",
    "LLM_PROVIDER",
    "LLM_MODEL",
]


def _read_env_content() -> tuple[str, str]:
    """读取环境变量内容。优先从文件读取，文件不存在时从 os.environ 格式化输出。

    Returns:
        (content, source): content 为文本内容，source 为 "file" 或 "environ"
    """
    # 优先 Docker 内挂载文件
    if _ENV_PATH.exists():
        return _ENV_PATH.read_text(encoding="utf-8"), "file"
    # 本地开发 .env 文件
    if _LOCAL_ENV_PATH.exists():
        return _LOCAL_ENV_PATH.read_text(encoding="utf-8"), "file"
    # 文件不存在（--env-file 注入模式），从 os.environ 读取
    lines = []
    for key in _DISPLAY_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            # 敏感字段脱敏：只显示前4位 + 星号
            if "KEY" in key or "PASSWORD" in key:
                display_value = value[:4] + "****" if len(value) > 4 else "****"
            else:
                display_value = value
            lines.append(f"{key}={display_value}")
    # 补充其他以 LAPLACE_ 开头的自定义变量
    for key, value in sorted(os.environ.items()):
        if key.startswith("LAPLACE_") and key not in _DISPLAY_ENV_KEYS:
            lines.append(f"{key}={value}")
    return "\n".join(lines) if lines else "# 未检测到已配置的环境变量", "environ"


# ── 环境变量管理 ──


@router.get("/env")
async def get_env():
    """读取环境变量内容。文件优先，文件不存在时从 os.environ 获取。"""
    content, source = _read_env_content()
    return {"content": content, "source": source}


class EnvUpdateRequest(BaseModel):
    content: str


@router.put("/env")
async def update_env(body: EnvUpdateRequest):
    """更新 .env 文件内容。仅文件模式下可写入。"""
    # 确定写入路径
    if _ENV_PATH.exists():
        env_path = _ENV_PATH
    elif _LOCAL_ENV_PATH.exists():
        env_path = _LOCAL_ENV_PATH
    else:
        raise HTTPException(
            status_code=400,
            detail="当前为 --env-file 注入模式，无 .env 文件实体可写入。"
            "如需持久化修改，请在部署时挂载 volume：-v /opt/laplace/.env:/app/.env",
        )
    env_path.write_text(body.content, encoding="utf-8")
    return {"ok": True, "message": ".env 已更新，需重启容器生效"}


@router.post("/restart")
async def restart_container():
    """通过 Docker Socket API 重启容器。"""
    container_name = os.getenv("CONTAINER_NAME", "laplace")
    socket_path = "/var/run/docker.sock"

    if not Path(socket_path).exists():
        raise HTTPException(
            status_code=503,
            detail="Docker Socket 不可用（本地开发环境无法重启容器）",
        )

    try:
        import httpx

        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport) as client:
            resp = await client.post(
                f"http://localhost/containers/{container_name}/restart",
                params={"t": 3},
                timeout=30.0,
            )
            if resp.status_code == 204:
                return {"ok": True, "message": f"容器 {container_name} 正在重启"}
            else:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Docker API 返回 {resp.status_code}: {resp.text}",
                )
    except ImportError:
        raise HTTPException(status_code=500, detail="缺少 httpx 依赖")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重启失败: {e}")


# ── 配置文件管理 ──

# 允许编辑的配置文件白名单
_ALLOWED_CONFIGS = {
    "nicknames.json",
    "translations.json",
    "effect_overrides.json",
    "uvicorn_log_config.json",
}


@router.get("/config")
async def list_configs():
    """列出所有可管理的配置文件。"""
    configs = []
    for f in sorted(_CONFIG_DIR.iterdir()):
        if f.suffix == ".json" and f.name in _ALLOWED_CONFIGS:
            configs.append(
                {
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                }
            )
    return {"configs": configs}


@router.get("/config/{filename}")
async def get_config(filename: str):
    """读取指定配置文件内容。"""
    if filename not in _ALLOWED_CONFIGS:
        raise HTTPException(status_code=403, detail=f"不允许访问 {filename}")
    filepath = _CONFIG_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"{filename} 不存在")
    content = filepath.read_text(encoding="utf-8")
    return {"name": filename, "content": content}


class ConfigUpdateRequest(BaseModel):
    content: str


@router.put("/config/{filename}")
async def update_config(filename: str, body: ConfigUpdateRequest):
    """更新配置文件（先校验 JSON 格式）。"""
    if filename not in _ALLOWED_CONFIGS:
        raise HTTPException(status_code=403, detail=f"不允许修改 {filename}")
    # JSON 格式校验
    try:
        json.loads(body.content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"JSON 格式错误: {e}")
    filepath = _CONFIG_DIR / filename
    filepath.write_text(body.content, encoding="utf-8")
    return {"ok": True, "message": f"{filename} 已更新（配置文件支持热更新，无需重启）"}
