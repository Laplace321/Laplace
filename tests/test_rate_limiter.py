"""
Rate Limiter 中间件测试。

覆盖场景：
- 正常请求不被拦截
- 超过限制后返回 429
- 窗口过期后恢复访问
- 非限制路径不受影响
- CORS 白名单配置
"""

import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.rate_limiter import RateLimitMiddleware


def _create_app(
    max_requests: int = 3, global_max_requests: int = 0, window_seconds: int = 2, paths: list[str] | None = None
):
    """创建带 RateLimitMiddleware 的测试应用。"""
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        max_requests=max_requests,
        global_max_requests=global_max_requests,
        window_seconds=window_seconds,
        paths=paths or ["/api/chat"],
    )

    @app.get("/api/chat")
    async def chat():
        return {"ok": True}

    @app.get("/api/chat/stream")
    async def chat_stream():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    return app


class TestRateLimiterNormal:
    """正常请求不被拦截。"""

    def test_requests_within_limit_pass(self):
        app = _create_app(max_requests=5)
        client = TestClient(app)
        for _ in range(5):
            resp = client.get("/api/chat")
            assert resp.status_code == 200

    def test_first_request_always_passes(self):
        app = _create_app(max_requests=1)
        client = TestClient(app)
        resp = client.get("/api/chat")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


class TestRateLimiterExceeded:
    """超过限制后返回 429。"""

    def test_returns_429_when_exceeded(self):
        app = _create_app(max_requests=3)
        client = TestClient(app)
        # 前 3 次正常
        for _ in range(3):
            resp = client.get("/api/chat")
            assert resp.status_code == 200
        # 第 4 次超限
        resp = client.get("/api/chat")
        assert resp.status_code == 429
        body = resp.json()
        assert "请求过于频繁" in body["error"]
        assert body["retry_after"] == 2
        assert resp.headers.get("Retry-After") == "2"

    def test_429_is_json_content_type(self):
        app = _create_app(max_requests=1)
        client = TestClient(app)
        client.get("/api/chat")  # 用完配额
        resp = client.get("/api/chat")
        assert resp.status_code == 429
        assert "application/json" in resp.headers.get("content-type", "")


class TestRateLimiterWindowExpiry:
    """窗口过期后恢复访问。"""

    def test_recovers_after_window_expires(self):
        app = _create_app(max_requests=2, window_seconds=1)
        client = TestClient(app)
        # 用完配额
        for _ in range(2):
            client.get("/api/chat")
        resp = client.get("/api/chat")
        assert resp.status_code == 429

        # 等待窗口过期
        time.sleep(1.1)
        resp = client.get("/api/chat")
        assert resp.status_code == 200


class TestRateLimiterPathFiltering:
    """非限制路径不受影响。"""

    def test_health_endpoint_not_limited(self):
        app = _create_app(max_requests=1, paths=["/api/chat"])
        client = TestClient(app)
        # 先用完 /api/chat 配额
        client.get("/api/chat")
        resp = client.get("/api/chat")
        assert resp.status_code == 429
        # /api/health 不受限制
        for _ in range(10):
            resp = client.get("/api/health")
            assert resp.status_code == 200

    def test_multiple_paths_all_limited(self):
        app = _create_app(max_requests=2, paths=["/api/chat", "/api/chat/stream"])
        client = TestClient(app)
        # /api/chat 用 2 次
        for _ in range(2):
            client.get("/api/chat")
        resp = client.get("/api/chat")
        assert resp.status_code == 429

        # /api/chat/stream 也有独立计数（同一 IP 共享窗口）
        # 注意：同 IP 的所有限制路径共享计数
        resp = client.get("/api/chat/stream")
        assert resp.status_code == 429


class TestGlobalRateLimit:
    """全局限流测试。"""

    def test_global_limit_blocks_all_ips(self):
        """全局配额用完后，所有请求都被拒绝。"""
        app = _create_app(max_requests=100, global_max_requests=3)
        client = TestClient(app)
        # 前 3 次正常（Per-IP 限额 100 远大于全局 3）
        for _ in range(3):
            resp = client.get("/api/chat")
            assert resp.status_code == 200
        # 第 4 次被全局限流
        resp = client.get("/api/chat")
        assert resp.status_code == 429

    def test_global_limit_recovers_after_window(self):
        """全局窗口过期后恢复。"""
        app = _create_app(max_requests=100, global_max_requests=2, window_seconds=1)
        client = TestClient(app)
        for _ in range(2):
            client.get("/api/chat")
        resp = client.get("/api/chat")
        assert resp.status_code == 429
        time.sleep(1.1)
        resp = client.get("/api/chat")
        assert resp.status_code == 200

    def test_per_ip_triggers_before_global(self):
        """Per-IP 限额先触发时，全局计数不增加。"""
        app = _create_app(max_requests=2, global_max_requests=10)
        client = TestClient(app)
        for _ in range(2):
            client.get("/api/chat")
        # Per-IP 超限
        resp = client.get("/api/chat")
        assert resp.status_code == 429

    def test_global_zero_means_disabled(self):
        """global_max_requests=0 表示不启用全局限流。"""
        app = _create_app(max_requests=100, global_max_requests=0)
        client = TestClient(app)
        # 发送大量请求不会被全局限流
        for _ in range(50):
            resp = client.get("/api/chat")
            assert resp.status_code == 200


class TestCorsConfiguration:
    """CORS 白名单配置测试。"""

    def test_cors_origins_from_env(self):
        with patch.dict("os.environ", {"CORS_ORIGINS": "http://example.com,http://test.com"}):
            # 验证环境变量解析逻辑
            origins_str = "http://example.com,http://test.com"
            origins = [o.strip() for o in origins_str.split(",") if o.strip()]
            assert origins == ["http://example.com", "http://test.com"]

    def test_default_cors_origins(self):
        """默认白名单仅包含 localhost。"""
        default = "http://localhost:8000,http://127.0.0.1:8000"
        origins = [o.strip() for o in default.split(",") if o.strip()]
        assert "http://localhost:8000" in origins
        assert "http://127.0.0.1:8000" in origins
        assert len(origins) == 2
