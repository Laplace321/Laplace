"""
Laplace — Admin 认证模块

静态密码 + Session Cookie 认证。
密码哈希存储在 .env（ADMIN_PASSWORD_HASH），登录后设 httpOnly signed cookie。
"""

import hashlib
import os
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

# ── 配置 ──
SESSION_TTL = 24 * 3600  # 24 小时过期
COOKIE_NAME = "laplace_admin_session"


def _get_password_hash() -> str:
    """延迟读取 ADMIN_PASSWORD_HASH，确保 .env 加载后才读取。"""
    return os.getenv("ADMIN_PASSWORD_HASH", "")


# ── 内存 Session Store ──
_sessions: dict[str, float] = {}  # token -> expire_timestamp

router = APIRouter(tags=["admin-auth"])


# ── 工具函数 ──


def hash_password(plain: str) -> str:
    """SHA256 哈希密码。"""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码与哈希是否匹配。"""
    return hashlib.sha256(plain.encode("utf-8")).hexdigest() == hashed


def create_session_token() -> str:
    """生成随机 session token 并存入 store。"""
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def _cleanup_expired():
    """清理过期 session（惰性清理）。"""
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp < now]
    for t in expired:
        del _sessions[t]


# ── FastAPI 依赖 ──


async def require_admin(request: Request):
    """校验 Admin 登录状态，未登录抛 401。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="未登录，请先登录管理后台")
    _cleanup_expired()
    if token not in _sessions or _sessions[token] < time.time():
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")


# ── 登录/登出 API ──


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def admin_login(body: LoginRequest, response: Response):
    """Admin 登录。"""
    pw_hash = _get_password_hash()
    if not pw_hash:
        raise HTTPException(status_code=500, detail="未配置管理员密码，请在 .env 设置 ADMIN_PASSWORD_HASH")
    if not verify_password(body.password, pw_hash):
        raise HTTPException(status_code=403, detail="密码错误")
    token = create_session_token()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        max_age=SESSION_TTL,
        path="/",
    )
    return {"ok": True, "message": "登录成功"}


@router.post("/logout")
async def admin_logout(response: Response):
    """Admin 登出。"""
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True, "message": "已登出"}


@router.get("/me")
async def admin_me(request: Request):
    """检查当前登录状态。"""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return {"logged_in": False}
    _cleanup_expired()
    if token not in _sessions or _sessions[token] < time.time():
        return {"logged_in": False}
    return {"logged_in": True}
