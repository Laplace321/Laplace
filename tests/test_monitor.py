"""
Laplace — 运维监控模块单元测试

覆盖 MetricsCollector、Alerter、/api/health、/metrics 端点。
"""

import time
from unittest.mock import patch

import pytest

from server.monitor.alerter import Alerter
from server.monitor.metrics import MetricsCollector

# ══════════════════════════════════════════
#  MetricsCollector 单元测试
# ══════════════════════════════════════════


class TestMetricsCollector:
    """MetricsCollector 指标采集与聚合测试。"""

    def test_record_llm_call_success(self):
        """记录成功的 LLM 调用后，get_summary 正确反映。"""
        collector = MetricsCollector()
        collector.record_llm_call("obao", "claude-sonnet", 150.0, success=True)
        collector.record_llm_call("obao", "claude-sonnet", 200.0, success=True)

        summary = collector.get_summary(minutes=5)
        assert summary["llm"]["calls"] == 2
        assert summary["llm"]["successes"] == 2
        assert summary["llm"]["errors"] == 0
        assert summary["llm"]["success_rate"] == 100.0
        assert summary["llm"]["avg_latency_ms"] == 175.0
        assert summary["llm"]["max_latency_ms"] == 200.0

    def test_record_llm_call_failure_and_fallback(self):
        """记录失败和降级调用后，计数器正确递增。"""
        collector = MetricsCollector()
        collector.record_llm_call("obao", "claude-sonnet", 100.0, success=True)
        collector.record_llm_call(
            "obao", "claude-sonnet", 5000.0, success=False, is_fallback=True, error_type="timeout"
        )
        collector.record_llm_call("dashscope", "qwen-max", 300.0, success=True, is_fallback=True)

        summary = collector.get_summary(minutes=5)
        assert summary["llm"]["calls"] == 3
        assert summary["llm"]["successes"] == 2
        assert summary["llm"]["errors"] == 1
        assert summary["llm"]["fallbacks"] == 2
        assert summary["llm"]["error_types"] == {"timeout": 1}

    def test_record_llm_call_model_breakdown(self):
        """模型级别的指标细分正确。"""
        collector = MetricsCollector()
        collector.record_llm_call("obao", "model-a", 100.0, success=True)
        collector.record_llm_call("obao", "model-a", 200.0, success=False, error_type="server_error")
        collector.record_llm_call("obao", "model-b", 50.0, success=True)

        summary = collector.get_summary(minutes=5)
        models = summary["models"]
        assert "model-a" in models
        assert models["model-a"]["calls"] == 2
        assert models["model-a"]["successes"] == 1
        assert models["model-a"]["errors"] == 1
        assert models["model-a"]["success_rate"] == 50.0
        assert models["model-b"]["calls"] == 1
        assert models["model-b"]["success_rate"] == 100.0

    def test_record_request(self):
        """HTTP 请求指标记录正确。"""
        collector = MetricsCollector()
        collector.record_request("/api/chat", 250.0, 200)
        collector.record_request("/api/chat", 300.0, 200)
        collector.record_request("/api/chat", 100.0, 429)

        summary = collector.get_summary(minutes=5)
        assert summary["http"]["requests"] == 3
        assert summary["http"]["status_codes"] == {200: 2, 429: 1}

    def test_model_available_gauge(self):
        """模型可用性 gauge 正确读写。"""
        collector = MetricsCollector()
        assert collector.get_model_status() == {}

        collector.set_model_available("claude-sonnet", True)
        collector.set_model_available("gpt-4o", False)
        status = collector.get_model_status()
        assert status == {"claude-sonnet": True, "gpt-4o": False}

    def test_get_summary_empty(self):
        """无数据时 get_summary 返回零值，不报错。"""
        collector = MetricsCollector()
        summary = collector.get_summary(minutes=5)
        assert summary["llm"]["calls"] == 0
        assert summary["llm"]["success_rate"] == 100.0
        assert summary["llm"]["avg_latency_ms"] == 0.0
        assert summary["http"]["requests"] == 0
        assert summary["http"]["avg_latency_ms"] == 0.0

    def test_totals_accumulate_across_minutes(self):
        """累计计数器不随桶过期而丢失。"""
        collector = MetricsCollector()
        collector.record_llm_call("p", "m", 100.0, success=True)
        collector.record_llm_call("p", "m", 100.0, success=False, error_type="timeout")

        summary = collector.get_summary(minutes=5)
        totals = summary["totals"]
        assert totals["llm_calls"] == 2
        assert totals["llm_successes"] == 1
        assert totals["llm_errors"] == 1

    def test_prometheus_text_format(self):
        """to_prometheus_text 输出合法 Prometheus 格式。"""
        collector = MetricsCollector()
        collector.record_llm_call("obao", "claude-sonnet", 100.0, success=True)
        collector.record_llm_call("obao", "claude-sonnet", 200.0, success=False, error_type="timeout")
        collector.set_model_available("claude-sonnet", True)
        collector.set_model_available("gpt-4o", False)

        text = collector.to_prometheus_text()

        # 基本格式校验
        assert "# HELP laplace_llm_requests_total" in text
        assert "# TYPE laplace_llm_requests_total counter" in text
        assert "laplace_llm_requests_total 2" in text
        assert "laplace_llm_successes_total 1" in text
        assert "laplace_llm_errors_total 1" in text

        # 模型可用性 gauge
        assert 'laplace_model_available{model="claude-sonnet"} 1' in text
        assert 'laplace_model_available{model="gpt-4o"} 0' in text

        # 5 分钟按模型调用量
        assert 'laplace_llm_calls_5m{model="claude-sonnet"} 2' in text

        # 每行格式校验：非注释行应符合 metric_name{labels} value 或 metric_name value
        for line in text.strip().split("\n"):
            if not line:
                continue
            if line.startswith("#"):
                assert line.startswith("# HELP ") or line.startswith("# TYPE ")
            else:
                parts = line.split(" ")
                assert len(parts) == 2, f"非法行: {line}"
                # value 应该是数字
                float(parts[1])

    def test_prometheus_text_empty(self):
        """无数据时 Prometheus 输出不报错，计数器全 0。"""
        collector = MetricsCollector()
        text = collector.to_prometheus_text()
        assert "laplace_llm_requests_total 0" in text
        assert "laplace_http_requests_total 0" in text


# ══════════════════════════════════════════
#  Alerter 单元测试
# ══════════════════════════════════════════


class TestAlerter:
    """Alerter 去重和静默跳过测试。"""

    def test_skip_without_config(self):
        """未配置 TELEGRAM_BOT_TOKEN 时静默跳过。"""
        with patch.dict("os.environ", {}, clear=False):
            # 确保环境变量不存在
            import os

            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("TELEGRAM_CHAT_ID", None)
            alerter = Alerter()
            assert not alerter.is_configured

    def test_configured_when_env_set(self):
        """配置了 TOKEN 和 CHAT_ID 时 is_configured 为 True。"""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "12345"}):
            alerter = Alerter()
            assert alerter.is_configured

    def test_dedup_same_key(self):
        """同一 alert_key 30 分钟内不重复发送。"""
        alerter = Alerter()
        assert alerter._should_send("model_down:claude") is True
        assert alerter._should_send("model_down:claude") is False
        assert alerter._should_send("model_down:claude") is False

    def test_dedup_different_keys(self):
        """不同 alert_key 互不影响。"""
        alerter = Alerter()
        assert alerter._should_send("model_down:claude") is True
        assert alerter._should_send("model_down:gpt-4o") is True

    def test_dedup_expires(self):
        """去重窗口过期后可以重新发送。"""
        alerter = Alerter()
        assert alerter._should_send("model_down:claude") is True

        # 手动修改记录时间到 31 分钟前
        alerter._sent_alerts["model_down:claude"] = time.time() - 31 * 60
        assert alerter._should_send("model_down:claude") is True

    @pytest.mark.asyncio
    async def test_send_alert_skip_unconfigured(self):
        """未配置时 send_alert 返回 False，不发送。"""
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            os.environ.pop("TELEGRAM_CHAT_ID", None)
            alerter = Alerter()
            result = await alerter.send_alert("CRITICAL", "Test", "test message")
            assert result is False

    @pytest.mark.asyncio
    async def test_send_alert_dedup_skip(self):
        """去重窗口内 send_alert 返回 False。"""
        with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "fake", "TELEGRAM_CHAT_ID": "123"}):
            alerter = Alerter()
            # 第一次应尝试发送（会因为假 token 失败，但逻辑上通过了去重检查）
            # mock _send_sync 避免真实网络调用
            with patch.object(alerter, "_send_sync", return_value=True):
                result1 = await alerter.send_alert("CRITICAL", "Test", "msg", alert_key="test_key")
                assert result1 is True
                result2 = await alerter.send_alert("CRITICAL", "Test", "msg", alert_key="test_key")
                assert result2 is False

    def test_format_message(self):
        """消息格式包含级别标记、标题和时间戳。"""
        alerter = Alerter()
        msg = alerter._format_message("CRITICAL", "模型不可用", "claude-sonnet 连续 2 次探活失败")
        assert "🔴" in msg
        assert "*CRITICAL*" in msg
        assert "模型不可用" in msg
        assert "Laplace Monitor" in msg


# ══════════════════════════════════════════
#  /api/health 和 /metrics 端点集成测试
# ══════════════════════════════════════════


class TestMonitorEndpoints:
    """API 端点集成测试（使用 FastAPI TestClient）。"""

    @pytest.fixture
    def client(self):
        """创建 TestClient，mock 掉 startup 中的数据库加载和探活。"""
        with (
            patch("server.main.load_database"),
            patch("server.main._validate_translations"),
            patch("server.main._validate_skill_registry"),
            patch("server.monitor.health_checker.start_probe_loop"),
        ):
            from fastapi.testclient import TestClient

            from server.main import app

            with TestClient(app) as tc:
                yield tc

    def _reset_collector(self):
        """重置全局 MetricsCollector 单例，避免测试间状态污染。"""
        import server.monitor.metrics as _metrics_mod

        _metrics_mod._collector = None

    def test_health_returns_models(self, client):
        """/api/health 返回 models 字段。"""
        self._reset_collector()
        from server.monitor.metrics import get_collector

        collector = get_collector()
        collector.set_model_available("claude-sonnet", True)
        collector.set_model_available("gpt-4o", False)

        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert data["models"]["claude-sonnet"] is True
        assert data["models"]["gpt-4o"] is False
        assert data["status"] == "degraded"

    def test_health_all_ok(self, client):
        """/api/health 所有模型可用时返回 status=ok。"""
        self._reset_collector()
        from server.monitor.metrics import get_collector

        collector = get_collector()
        collector.set_model_available("claude-sonnet", True)

        resp = client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"

    def test_metrics_endpoint(self, client):
        """/metrics 返回 Prometheus text format。"""
        self._reset_collector()
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")

        body = resp.text
        assert "laplace_llm_requests_total" in body
        assert "# HELP" in body
        assert "# TYPE" in body
