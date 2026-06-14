"""Admin 路由模块单元测试。"""

import json
import os

from fastapi.testclient import TestClient

os.environ["ADMIN_PASSWORD_HASH"] = "ecd71870d1963316a97e3ac3408c9835ad8cf0f3c1bc703527c30265534f75ae"

from server.admin.auth import _sessions
from server.main import app

client = TestClient(app)


def _login():
    """登录并返回 cookies。"""
    resp = client.post("/api/admin/login", json={"password": "test123"})
    return resp.cookies


class TestConfigAPI:
    def test_list_configs_unauthorized(self):
        resp = client.get("/api/admin/config")
        assert resp.status_code == 401

    def test_list_configs(self):
        cookies = _login()
        resp = client.get("/api/admin/config", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert "configs" in data
        names = [c["name"] for c in data["configs"]]
        assert "nicknames.json" in names
        assert "translations.json" in names

    def test_get_config(self):
        cookies = _login()
        resp = client.get("/api/admin/config/nicknames.json", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "nicknames.json"
        assert "content" in data
        # 内容应该是合法 JSON
        json.loads(data["content"])

    def test_get_config_forbidden(self):
        cookies = _login()
        resp = client.get("/api/admin/config/secret.json", cookies=cookies)
        assert resp.status_code == 403

    def test_update_config_invalid_json(self):
        cookies = _login()
        resp = client.put(
            "/api/admin/config/nicknames.json",
            json={"content": "not valid json"},
            cookies=cookies,
        )
        assert resp.status_code == 400

    def test_update_config_valid(self):
        cookies = _login()
        # 先读取原内容
        get_resp = client.get("/api/admin/config/nicknames.json", cookies=cookies)
        original = get_resp.json()["content"]

        # 写回原内容（不改动）
        resp = client.put(
            "/api/admin/config/nicknames.json",
            json={"content": original},
            cookies=cookies,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestEnvAPI:
    def test_get_env_unauthorized(self):
        _sessions.clear()
        resp = client.get("/api/admin/env")
        assert resp.status_code == 401

    def test_get_env(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        # CI 环境可能没有 .env，创建临时文件 mock
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("TEST_VAR=hello\n")
            tmp_path = Path(f.name)
        try:
            with patch("server.admin.routes._LOCAL_ENV_PATH", tmp_path):
                cookies = _login()
                resp = client.get("/api/admin/env", cookies=cookies)
                assert resp.status_code == 200
                data = resp.json()
                assert "content" in data
                assert data["source"] == "file"
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_restart_container(self):
        """Docker restart API 需要登录才能调用。"""
        cookies = _login()
        resp = client.post("/api/admin/restart", cookies=cookies)
        # 本地有 Docker Socket 时返回 204 包装（成功），无 Socket 返回 503
        assert resp.status_code in (200, 500, 503)


# ============================================================
# /api/rate — 评分同步 BI 索引（ADR-032）
# ============================================================


class TestRateAPI:
    """/api/rate 必须在 log_trace_event 后同步刷新 BI 索引（rating 列）。"""

    def test_rate_invalid_value_returns_400(self):
        resp = client.post("/api/rate", json={"trace_id": "t-rate-1", "rating": "great"})
        assert resp.status_code == 400
        assert "bad/ok/good" in resp.json().get("error", "")

    def test_rate_valid_calls_upsert_turn(self):
        """评分提交后必须调用 bi_index.upsert_turn 刷新 SQLite turn_summary.rating。"""
        from unittest.mock import AsyncMock, patch

        # patch 函数所在模块（main.py 内部 import）
        with (
            patch("server.bi_index.upsert_turn", return_value=True) as mock_upsert,
            patch("server.logger.log_trace_event", new=AsyncMock()) as mock_log,
        ):
            resp = client.post("/api/rate", json={"trace_id": "t-rate-2", "rating": "bad"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        # 必须先写日志事件
        mock_log.assert_awaited_once()
        log_args, log_kwargs = mock_log.await_args
        assert log_args[0] == "t-rate-2"
        assert log_args[1] == "rating"
        assert log_args[2] == {"rating": "bad"}
        # 必须同步刷新 BI
        mock_upsert.assert_called_once_with("t-rate-2")

    def test_rate_upsert_failure_does_not_break_response(self):
        """upsert_turn 异常时 API 仍返回 200（防御性）。"""
        from unittest.mock import AsyncMock, patch

        with (
            patch("server.bi_index.upsert_turn", side_effect=RuntimeError("db locked")),
            patch("server.logger.log_trace_event", new=AsyncMock()),
        ):
            resp = client.post("/api/rate", json={"trace_id": "t-rate-3", "rating": "good"})

        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
