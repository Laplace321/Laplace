"""从者头像代理路由测试。"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.face_proxy import _ascension_fallback_candidates
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
        """Atlas 返回 404 且无降级候选时返回 404。"""
        mock_response = AsyncMock()
        mock_response.status_code = 404

        with patch("server.face_proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # f_0000000.png 不匹配灵基模式，无降级候选
            resp = client.get("/faces/f_0000000.png")
            assert resp.status_code == 404


class TestAscensionFallback:
    """灵基降级逻辑测试。"""

    def test_fallback_candidates_from_asc3(self):
        """灵基3降级到2和1。"""
        candidates = _ascension_fallback_candidates("f_10022003.png")
        assert candidates == ["f_10022002.png", "f_10022001.png"]

    def test_fallback_candidates_from_asc4(self):
        """灵基4降级到3、2、1。"""
        candidates = _ascension_fallback_candidates("f_10011004.png")
        assert candidates == ["f_10011003.png", "f_10011002.png", "f_10011001.png"]

    def test_fallback_candidates_from_asc1(self):
        """灵基1无降级候选。"""
        candidates = _ascension_fallback_candidates("f_10022001.png")
        assert candidates == []

    def test_fallback_candidates_non_matching(self):
        """非标准文件名无降级候选。"""
        assert _ascension_fallback_candidates("f_0000000.png") == []
        assert _ascension_fallback_candidates("random.png") == []

    def test_proxy_ascension_fallback(self, _setup_faces_dir):
        """原始灵基404时降级到低灵基。"""
        faces_dir = _setup_faces_dir
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

        # 模拟: f_10022003 返回 404，f_10022002 返回 404，f_10022001 返回 200
        mock_404 = AsyncMock()
        mock_404.status_code = 404

        mock_200 = AsyncMock()
        mock_200.status_code = 200
        mock_200.content = fake_png
        mock_200.url = "https://static.atlasacademy.io/JP/Faces/f_10022001.png"

        call_count = {"n": 0}

        async def mock_get(url):
            call_count["n"] += 1
            if "f_10022001" in url:
                return mock_200
            return mock_404

        with patch("server.face_proxy.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            resp = client.get("/faces/f_10022003.png")
            assert resp.status_code == 200
            assert resp.content == fake_png
            # 验证使用原始文件名缓存（下次直接命中）
            assert (faces_dir / "f_10022003.png").exists()
            # 调用了3次: 原始(404) + 降级2(404) + 降级1(200)
            assert call_count["n"] == 3
