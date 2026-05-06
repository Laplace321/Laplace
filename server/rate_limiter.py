"""
Laplace — IP Rate Limiter Middleware

零依赖的轻量 IP 速率限制中间件，基于内存滑动窗口实现。
仅对指定路径生效，超限返回 HTTP 429。
"""

import time
import json
import logging
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("LaplaceTracer")

# 清理间隔：每 60 秒清理一次过期记录
_CLEANUP_INTERVAL = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    """基于滑动窗口的 IP 速率限制中间件。

    Args:
        app: ASGI 应用
        max_requests: 窗口内允许的最大请求数（默认 30）
        window_seconds: 滑动窗口大小（秒，默认 60）
        paths: 需要限制的路径前缀列表（默认 ["/api/chat"]）
    """

    def __init__(self, app, max_requests: int = 30, window_seconds: int = 60, paths: list[str] | None = None):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.paths = paths or ["/api/chat"]
        # IP -> deque of timestamps
        self.requests: dict[str, deque] = defaultdict(deque)
        self._last_cleanup = time.monotonic()

    def _should_limit(self, path: str) -> bool:
        """检查路径是否在限制列表中。"""
        return any(path == p or path.startswith(p + "/") for p in self.paths)

    def _get_client_ip(self, request: Request) -> str:
        """获取客户端 IP，支持代理转发。"""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _cleanup_expired(self, now: float) -> None:
        """定期清理过期记录，防止内存泄漏。"""
        if now - self._last_cleanup < _CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        expired_ips = [
            ip for ip, timestamps in self.requests.items()
            if not timestamps or timestamps[-1] < now - self.window_seconds
        ]
        for ip in expired_ips:
            del self.requests[ip]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 仅对指定路径限流
        if not self._should_limit(path):
            return await call_next(request)

        now = time.monotonic()
        client_ip = self._get_client_ip(request)

        # 清理过期记录
        self._cleanup_expired(now)

        # 移除窗口外的请求记录
        timestamps = self.requests[client_ip]
        while timestamps and timestamps[0] <= now - self.window_seconds:
            timestamps.popleft()

        # 检查是否超限
        if len(timestamps) >= self.max_requests:
            logger.warning({
                "event": "rate_limit_exceeded",
                "client_ip": client_ip,
                "path": path,
                "requests_in_window": len(timestamps),
                "limit": self.max_requests,
            })
            return Response(
                content=json.dumps(
                    {"error": "请求过于频繁，请稍后再试", "retry_after": self.window_seconds},
                    ensure_ascii=False,
                ),
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(self.window_seconds)},
            )

        # 记录本次请求
        timestamps.append(now)
        return await call_next(request)
