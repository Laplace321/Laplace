"""从者头像代理路由测试。"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_faces_dir(tmp_path, monkeypatch):
    """使用临时目录替代真实 faces 目录。"""
    faces_dir = tmp_path / "faces"
    faces_dir.mkdir()
    monkeypatch.setattr("server.face_proxy.FACES_DIR", faces_dir)
    return faces_dir


class TestFaceProxyLocalHit:
    """本地文件存在时直接返回。"""

    def test_returns_png_from_local(self, _setup_faces_dir):
        """本地存在图片时返回 200 + image/png。"""
        faces_dir = _setup_faces_dir
        # 创建一个假 PNG 文件（PNG magic bytes）
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        (faces_dir / "f_1001003.png").write_bytes(fake_png)

        resp = client.get("/faces/f_1001003.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert "max-age=2592000" in resp.headers.get("cache-control", "")
        assert resp.content == fake_png

    def test_invalid_filename_rejected(self):
        """路径遍历尝试被拒绝。"""
        resp = client.get("/faces/../etc/passwd")
        assert resp.status_code in (400, 404, 422)

    def test_non_png_rejected(self):
        """非 PNG 文件被拒绝。"""
        resp = client.get("/faces/malicious.js")
        assert resp.status_code in (400, 422)


class TestFaceProxyFallback:
    """本地不存在时代理回源 Atlas CDN。"""

    def test_proxy_fallback_success(self, _setup_faces_dir):
        """本地不存在时从 Atlas 下载并缓存。"""
        faces_dir = _setup_faces_dir
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.content = fake_png
        mock_response.raise_for_status = lambda: None

        with patch("server.face_proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = client.get("/faces/f_9999999.png")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "image/png"
            assert resp.content == fake_png

            # 验证文件被缓存到本地
            assert (faces_dir / "f_9999999.png").exists()
            assert (faces_dir / "f_9999999.png").read_bytes() == fake_png

    def test_proxy_fallback_404(self, _setup_faces_dir):
        """Atlas 返回 404 时代理也返回 404。"""
        import httpx

        def _raise_404():
            raise httpx.HTTPStatusError(
                "Not Found",
                request=httpx.Request("GET", "http://test"),
                response=httpx.Response(404),
            )

        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_response.raise_for_status = _raise_404

        with patch("server.face_proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = client.get("/faces/f_0000000.png")
            assert resp.status_code == 404
