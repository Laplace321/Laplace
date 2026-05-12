"""Admin 认证模块单元测试。"""

import os
import time

from fastapi.testclient import TestClient

# 设置测试密码哈希（密码: test123）
os.environ["ADMIN_PASSWORD_HASH"] = "ecd71870d1963316a97e3ac3408c9835ad8cf0f3c1bc703527c30265534f75ae"

from server.admin.auth import (
    COOKIE_NAME,
    _sessions,
    create_session_token,
    hash_password,
    verify_password,
)
from server.main import app

client = TestClient(app)


class TestPasswordUtils:
    def test_hash_password(self):
        h = hash_password("test123")
        assert len(h) == 64  # SHA256 hex digest
        assert h == "ecd71870d1963316a97e3ac3408c9835ad8cf0f3c1bc703527c30265534f75ae"

    def test_verify_password_correct(self):
        h = hash_password("test123")
        assert verify_password("test123", h) is True

    def test_verify_password_wrong(self):
        h = hash_password("test123")
        assert verify_password("wrong", h) is False


class TestSessionToken:
    def test_create_session_token(self):
        token = create_session_token()
        assert token in _sessions
        assert _sessions[token] > time.time()

    def test_session_expiry(self):
        token = create_session_token()
        # 手动设置为已过期
        _sessions[token] = time.time() - 1
        assert _sessions[token] < time.time()


class TestLoginAPI:
    def test_login_success(self):
        resp = client.post("/api/admin/login", json={"password": "test123"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert COOKIE_NAME in resp.cookies

    def test_login_wrong_password(self):
        resp = client.post("/api/admin/login", json={"password": "wrong"})
        assert resp.status_code == 403

    def test_me_not_logged_in(self):
        # 清空 session store 确保无残留
        _sessions.clear()
        resp = client.get("/api/admin/me")
        assert resp.json()["logged_in"] is False

    def test_me_logged_in(self):
        login_resp = client.post("/api/admin/login", json={"password": "test123"})
        cookies = login_resp.cookies
        resp = client.get("/api/admin/me", cookies=cookies)
        assert resp.json()["logged_in"] is True

    def test_logout(self):
        login_resp = client.post("/api/admin/login", json={"password": "test123"})
        cookies = login_resp.cookies
        resp = client.post("/api/admin/logout", cookies=cookies)
        assert resp.status_code == 200


class TestRequireAdmin:
    def test_protected_endpoint_without_login(self):
        _sessions.clear()
        resp = client.get("/api/admin/logs")
        assert resp.status_code == 401

    def test_protected_endpoint_with_login(self):
        login_resp = client.post("/api/admin/login", json={"password": "test123"})
        cookies = login_resp.cookies
        resp = client.get("/api/admin/logs", cookies=cookies)
        assert resp.status_code == 200
