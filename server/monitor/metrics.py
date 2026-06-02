"""
Laplace — 指标采集器

内存环形缓冲，按分钟聚合 LLM 调用和 HTTP 请求指标。
集成被动告警：按 provider 记录连续失败计数，达到阈值触发 Warning 告警。
无外部依赖，线程安全。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class _MinuteBucket:
    """单分钟聚合桶。"""

    llm_calls: int = 0
    llm_successes: int = 0
    llm_fallbacks: int = 0
    llm_errors: int = 0
    llm_latency_sum_ms: float = 0.0
    llm_latency_max_ms: float = 0.0
    # 按 model 细分
    model_calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    model_successes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    model_errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    model_latency_sum_ms: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    # 按 error_type 细分
    error_types: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # HTTP 请求
    http_requests: int = 0
    http_latency_sum_ms: float = 0.0
    http_status_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))


class MetricsCollector:
    """内存指标采集器（单例）。

    按分钟聚合，保留最近 60 分钟数据。
    所有操作线程安全。
    """

    _RETENTION_MINUTES = 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[int, _MinuteBucket] = {}
        # 模型可用性 gauge（由 health_checker 更新）
        self._model_available: dict[str, bool] = {}
        # 累计计数器（不随桶过期）
        self._total_llm_calls = 0
        self._total_llm_successes = 0
        self._total_llm_fallbacks = 0
        self._total_llm_errors = 0
        self._total_http_requests = 0
        # ── 被动告警：按 provider 连续失败计数 ──
        self._consecutive_failures: dict[str, int] = {}
        self._alert_threshold = int(os.getenv("ALERT_CONSECUTIVE_THRESHOLD", "5"))
        # 已经触发过 Warning 的 provider（成功后重置）
        self._warned_providers: set[str] = set()

    def _current_minute(self) -> int:
        return int(time.time()) // 60

    def _get_bucket(self, minute_key: int) -> _MinuteBucket:
        if minute_key not in self._buckets:
            self._buckets[minute_key] = _MinuteBucket()
            self._gc()
        return self._buckets[minute_key]

    def _gc(self) -> None:
        """清理超出保留期的旧桶。"""
        cutoff = self._current_minute() - self._RETENTION_MINUTES
        stale_keys = [k for k in self._buckets if k < cutoff]
        for key in stale_keys:
            del self._buckets[key]

    @staticmethod
    def _fire_and_forget(coro) -> None:
        """安全地调度异步协程，无 event loop 时静默跳过（兼容同步测试环境）。"""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # 没有运行中的 event loop（同步测试环境），关闭协程避免 warning
            coro.close()

    # ── 记录 LLM 调用 ──

    def record_llm_call(
        self,
        provider: str,
        model: str,
        latency_ms: float,
        *,
        success: bool = True,
        is_fallback: bool = False,
        error_type: str = "",
    ) -> None:
        """记录一次 LLM 调用。

        成功时重置该 provider 连续失败计数；若之前处于告警状态，发送恢复通知。
        失败时递增连续失败计数；达到阈值触发 Warning 告警（30 分钟内去重）。
        """
        should_send_recovery = False
        should_send_warning = False
        fail_count = 0

        with self._lock:
            bucket = self._get_bucket(self._current_minute())
            bucket.llm_calls += 1
            self._total_llm_calls += 1

            bucket.model_calls[model] += 1
            bucket.llm_latency_sum_ms += latency_ms
            bucket.llm_latency_max_ms = max(bucket.llm_latency_max_ms, latency_ms)
            bucket.model_latency_sum_ms[model] += latency_ms

            if success:
                bucket.llm_successes += 1
                self._total_llm_successes += 1
                bucket.model_successes[model] += 1
                # 重置连续失败计数
                if self._consecutive_failures.get(provider, 0) > 0:
                    self._consecutive_failures[provider] = 0
                # 如果之前触发过 Warning，发恢复通知
                if provider in self._warned_providers:
                    self._warned_providers.discard(provider)
                    should_send_recovery = True
            else:
                bucket.llm_errors += 1
                self._total_llm_errors += 1
                bucket.model_errors[model] += 1
                if error_type:
                    bucket.error_types[error_type] += 1
                # 递增连续失败计数
                self._consecutive_failures[provider] = self._consecutive_failures.get(provider, 0) + 1
                fail_count = self._consecutive_failures[provider]
                # 达到阈值触发 Warning
                if fail_count >= self._alert_threshold and provider not in self._warned_providers:
                    self._warned_providers.add(provider)
                    should_send_warning = True

            if is_fallback:
                bucket.llm_fallbacks += 1
                self._total_llm_fallbacks += 1

        # 告警发送放在锁外，避免阻塞
        if should_send_recovery:
            self._fire_and_forget(self._send_recovery_alert(provider))
        if should_send_warning:
            self._fire_and_forget(self._send_warning_alert(provider, fail_count))

    # ── 记录 HTTP 请求 ──

    def record_request(self, path: str, latency_ms: float, status_code: int) -> None:
        """记录一次 HTTP 请求。"""
        with self._lock:
            bucket = self._get_bucket(self._current_minute())
            bucket.http_requests += 1
            self._total_http_requests += 1
            bucket.http_latency_sum_ms += latency_ms
            bucket.http_status_counts[status_code] += 1

    # ── 模型可用性（由 health_checker 更新）──

    def set_model_available(self, model: str, available: bool) -> None:
        """设置模型可用性 gauge。"""
        with self._lock:
            self._model_available[model] = available

    def get_model_status(self) -> dict[str, bool]:
        """返回各模型当前可用状态。"""
        with self._lock:
            return dict(self._model_available)

    # ── 查询汇总 ──

    def get_summary(self, minutes: int = 5) -> dict:
        """返回指定时间窗口的汇总指标。"""
        with self._lock:
            now_minute = self._current_minute()
            start_minute = now_minute - minutes + 1

            total_llm_calls = 0
            total_llm_successes = 0
            total_llm_fallbacks = 0
            total_llm_errors = 0
            total_llm_latency_ms = 0.0
            max_latency_ms = 0.0
            model_stats: dict[str, dict] = {}
            error_type_counts: dict[str, int] = defaultdict(int)
            total_http = 0
            total_http_latency_ms = 0.0
            http_status: dict[int, int] = defaultdict(int)

            for minute_key in range(start_minute, now_minute + 1):
                bucket = self._buckets.get(minute_key)
                if bucket is None:
                    continue

                total_llm_calls += bucket.llm_calls
                total_llm_successes += bucket.llm_successes
                total_llm_fallbacks += bucket.llm_fallbacks
                total_llm_errors += bucket.llm_errors
                total_llm_latency_ms += bucket.llm_latency_sum_ms
                max_latency_ms = max(max_latency_ms, bucket.llm_latency_max_ms)

                for model, count in bucket.model_calls.items():
                    if model not in model_stats:
                        model_stats[model] = {"calls": 0, "successes": 0, "errors": 0, "latency_sum_ms": 0.0}
                    model_stats[model]["calls"] += count
                    model_stats[model]["successes"] += bucket.model_successes.get(model, 0)
                    model_stats[model]["errors"] += bucket.model_errors.get(model, 0)
                    model_stats[model]["latency_sum_ms"] += bucket.model_latency_sum_ms.get(model, 0.0)

                for error_type, count in bucket.error_types.items():
                    error_type_counts[error_type] += count

                total_http += bucket.http_requests
                total_http_latency_ms += bucket.http_latency_sum_ms
                for code, count in bucket.http_status_counts.items():
                    http_status[code] += count

            # 计算派生指标
            success_rate = (total_llm_successes / total_llm_calls * 100) if total_llm_calls > 0 else 100.0
            avg_latency_ms = (total_llm_latency_ms / total_llm_calls) if total_llm_calls > 0 else 0.0

            # 模型级别汇总
            for stats in model_stats.values():
                calls = stats["calls"]
                stats["avg_latency_ms"] = round(stats["latency_sum_ms"] / calls, 1) if calls > 0 else 0.0
                stats["success_rate"] = round(stats["successes"] / calls * 100, 1) if calls > 0 else 100.0
                del stats["latency_sum_ms"]

            return {
                "window_minutes": minutes,
                "llm": {
                    "calls": total_llm_calls,
                    "successes": total_llm_successes,
                    "errors": total_llm_errors,
                    "fallbacks": total_llm_fallbacks,
                    "success_rate": round(success_rate, 1),
                    "avg_latency_ms": round(avg_latency_ms, 1),
                    "max_latency_ms": round(max_latency_ms, 1),
                    "error_types": dict(error_type_counts),
                },
                "models": model_stats,
                "model_available": dict(self._model_available),
                "http": {
                    "requests": total_http,
                    "avg_latency_ms": round(total_http_latency_ms / total_http, 1) if total_http > 0 else 0.0,
                    "status_codes": dict(http_status),
                },
                "totals": {
                    "llm_calls": self._total_llm_calls,
                    "llm_successes": self._total_llm_successes,
                    "llm_fallbacks": self._total_llm_fallbacks,
                    "llm_errors": self._total_llm_errors,
                    "http_requests": self._total_http_requests,
                },
            }

    # ── Prometheus text format ──

    def to_prometheus_text(self) -> str:
        """输出 Prometheus text exposition format。"""
        lines: list[str] = []

        with self._lock:
            # 累计计数器
            lines.append("# HELP laplace_llm_requests_total Total LLM API calls")
            lines.append("# TYPE laplace_llm_requests_total counter")
            lines.append(f"laplace_llm_requests_total {self._total_llm_calls}")

            lines.append("# HELP laplace_llm_successes_total Successful LLM API calls")
            lines.append("# TYPE laplace_llm_successes_total counter")
            lines.append(f"laplace_llm_successes_total {self._total_llm_successes}")

            lines.append("# HELP laplace_llm_errors_total Failed LLM API calls")
            lines.append("# TYPE laplace_llm_errors_total counter")
            lines.append(f"laplace_llm_errors_total {self._total_llm_errors}")

            lines.append("# HELP laplace_llm_fallbacks_total LLM fallback events")
            lines.append("# TYPE laplace_llm_fallbacks_total counter")
            lines.append(f"laplace_llm_fallbacks_total {self._total_llm_fallbacks}")

            lines.append("# HELP laplace_http_requests_total Total HTTP requests")
            lines.append("# TYPE laplace_http_requests_total counter")
            lines.append(f"laplace_http_requests_total {self._total_http_requests}")

            # 模型可用性 gauge
            lines.append("# HELP laplace_model_available Model availability (1=up, 0=down)")
            lines.append("# TYPE laplace_model_available gauge")
            for model, available in sorted(self._model_available.items()):
                lines.append(f'laplace_model_available{{model="{model}"}} {1 if available else 0}')

            # 最近 5 分钟按模型的调用量（作为 gauge 快照）
            now_minute = self._current_minute()
            model_calls_5m: dict[str, int] = defaultdict(int)
            model_errors_5m: dict[str, int] = defaultdict(int)
            for minute_key in range(now_minute - 4, now_minute + 1):
                bucket = self._buckets.get(minute_key)
                if bucket is None:
                    continue
                for model, count in bucket.model_calls.items():
                    model_calls_5m[model] += count
                for model, count in bucket.model_errors.items():
                    model_errors_5m[model] += count

            lines.append("# HELP laplace_llm_calls_5m LLM calls in last 5 minutes by model")
            lines.append("# TYPE laplace_llm_calls_5m gauge")
            for model, count in sorted(model_calls_5m.items()):
                lines.append(f'laplace_llm_calls_5m{{model="{model}"}} {count}')

            lines.append("# HELP laplace_llm_errors_5m LLM errors in last 5 minutes by model")
            lines.append("# TYPE laplace_llm_errors_5m gauge")
            for model, count in sorted(model_errors_5m.items()):
                lines.append(f'laplace_llm_errors_5m{{model="{model}"}} {count}')

        lines.append("")  # trailing newline
        return "\n".join(lines)

    # ── 被动告警方法 ──

    async def _send_warning_alert(self, provider: str, fail_count: int) -> None:
        """发送 Warning 告警：前置 provider 连续失败达到阈值。"""
        from server.monitor.alerter import AlertLevel, get_alerter

        alerter = get_alerter()
        await alerter.send_alert(
            level=AlertLevel.WARNING,
            title=f"Provider {provider} 连续失败",
            message=f"提供商 `{provider}` 已连续 {fail_count} 次业务调用失败，请关注",
            alert_key=f"warning:{provider}",
        )

    async def _send_recovery_alert(self, provider: str) -> None:
        """发送恢复通知：provider 从告警状态恢复。"""
        from server.monitor.alerter import AlertLevel, get_alerter

        alerter = get_alerter()
        await alerter.send_alert(
            level=AlertLevel.RECOVERY,
            title=f"Provider {provider} 已恢复",
            message=f"提供商 `{provider}` 已恢复正常",
        )

    async def record_all_providers_failed(self, attempts_log: list[dict]) -> None:
        """全链路失败告警：所有 provider 所有 model 都失败。

        Critical 级别，每次都推送（无去重）。
        """
        from server.monitor.alerter import AlertLevel, get_alerter

        alerter = get_alerter()
        # 构建简要的失败摘要
        summary_lines = []
        for attempt in attempts_log[-5:]:  # 最多展示最近 5 条
            summary_lines.append(
                f"  · {attempt.get('provider', '?')}/{attempt.get('model', '?')}: "
                f"{attempt.get('error_type', 'unknown')}"
            )
        summary = "\n".join(summary_lines) if summary_lines else "无详细记录"
        await alerter.send_alert(
            level=AlertLevel.CRITICAL,
            title="全链路失败",
            message=f"所有提供商全部失败，影响线上查询！\n\n最近尝试:\n{summary}",
            # 不传 alert_key → 不去重，每次都推送
        )

    def get_alert_history(self) -> list[dict]:
        """返回告警历史列表（委托给 Alerter）。"""
        from server.monitor.alerter import get_alerter

        return get_alerter().get_alert_history()


# ── 单例 ──

_collector: MetricsCollector | None = None
_collector_lock = threading.Lock()


def get_collector() -> MetricsCollector:
    """获取全局 MetricsCollector 单例。"""
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = MetricsCollector()
    return _collector
