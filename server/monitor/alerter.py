"""
Laplace — 告警推送（Bark + Telegram）

支持两种推送通道：
1. Bark（iOS 推送，优先）— 通过 BARK_URL 配置
2. Telegram Bot（备选）— 通过 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 配置

告警去重、告警历史记录、AlertLevel 分级。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from enum import StrEnum

# 北京时间
_BEIJING_TZ = timezone(timedelta(hours=8))

# 告警去重窗口（秒）
_DEDUP_WINDOW_SECONDS = 30 * 60  # 30 分钟

# 告警历史最大保留条数
_MAX_ALERT_HISTORY = 100

# 告警关联 trace_id 缓冲区上限（v0.5.1）
_MAX_RECENT_FAILURE_TRACES = 5


class AlertLevel(StrEnum):
    """告警级别。"""

    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    RECOVERY = "RECOVERY"


class Alerter:
    """多通道告警推送器（Bark 优先，Telegram 备选）。"""

    def __init__(self) -> None:
        # Bark 配置
        self._bark_url = os.getenv("BARK_URL", "").rstrip("/")
        # Telegram 配置
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        # 去重记录：alert_key -> last_sent_timestamp
        self._sent_alerts: dict[str, float] = {}
        # 告警历史（最近 N 条）
        self._alert_history: list[dict] = []
        # v0.5.1：失败 trace_id 缓冲区（FIFO，去重，发送告警时一次性消费并渲染）
        self._recent_failure_traces: list[str] = []

    def push_failure_trace(self, trace_id: str) -> None:
        """记录一次失败请求的 trace_id（``metrics.record_llm_call`` 失败分支调用）。

        - 自动去重 + FIFO 截断（保留最近 ``_MAX_RECENT_FAILURE_TRACES`` 个）
        - 空字符串 / "unknown" 跳过
        """
        if not trace_id or trace_id == "unknown":
            return
        if trace_id in self._recent_failure_traces:
            return
        self._recent_failure_traces.append(trace_id)
        if len(self._recent_failure_traces) > _MAX_RECENT_FAILURE_TRACES:
            self._recent_failure_traces = self._recent_failure_traces[-_MAX_RECENT_FAILURE_TRACES:]

    def consume_recent_failure_traces(self) -> list[str]:
        """读取并清空当前缓冲区，返回截至此刻积累的失败 trace_id（最旧→最新）。"""
        traces = list(self._recent_failure_traces)
        self._recent_failure_traces.clear()
        return traces

    @property
    def bark_configured(self) -> bool:
        return bool(self._bark_url)

    @property
    def telegram_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    @property
    def is_configured(self) -> bool:
        return self.bark_configured or self.telegram_configured

    def _should_send(self, alert_key: str) -> bool:
        """检查是否应该发送（去重判断）。"""
        now = time.time()
        last_sent = self._sent_alerts.get(alert_key, 0)
        if now - last_sent < _DEDUP_WINDOW_SECONDS:
            return False
        self._sent_alerts[alert_key] = now
        # 清理过期的去重记录
        cutoff = now - _DEDUP_WINDOW_SECONDS
        self._sent_alerts = {k: v for k, v in self._sent_alerts.items() if v > cutoff}
        return True

    def _record_history(self, level: str, title: str, body: str, channel: str, success: bool) -> None:
        """记录一条告警历史。"""
        now_str = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        entry = {
            "time": now_str,
            "level": level,
            "title": title,
            "body": body,
            "channel": channel,
            "success": success,
        }
        self._alert_history.insert(0, entry)
        # 保留最近 N 条
        if len(self._alert_history) > _MAX_ALERT_HISTORY:
            self._alert_history = self._alert_history[:_MAX_ALERT_HISTORY]

    def get_alert_history(self) -> list[dict]:
        """返回告警历史列表（最新在前）。"""
        return list(self._alert_history)

    # ── Bark 推送 ──

    def _send_bark_sync(self, title: str, body: str, group: str, level: str) -> bool:
        """同步发送 Bark 推送。"""
        level_emoji = {
            AlertLevel.CRITICAL: "🔴",
            AlertLevel.WARNING: "🟡",
            AlertLevel.RECOVERY: "🟢",
        }.get(level, "⚪")
        full_title = f"{level_emoji} [{level}] {title}"

        payload = json.dumps(
            {
                "title": full_title,
                "body": body,
                "group": group,
                "sound": "alarm" if level == AlertLevel.CRITICAL else "default",
                "level": "timeSensitive" if level == AlertLevel.CRITICAL else "active",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            self._bark_url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError) as exc:
            print(f"⚠️  Bark 告警发送失败: {exc}")
            return False

    # ── Telegram 推送 ──

    def _format_telegram_message(self, level: str, title: str, message: str) -> str:
        """格式化 Telegram 消息（纯文本，避免特殊字符导致 parse 失败）。"""
        now_str = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        level_emoji = {
            AlertLevel.CRITICAL: "🔴",
            AlertLevel.WARNING: "🟡",
            AlertLevel.RECOVERY: "🟢",
        }.get(level, "⚪")
        return f"{level_emoji} {level} | {title}\n\n{message}\n\n— Laplace Monitor · {now_str}"

    def _send_telegram_sync(self, text: str) -> bool:
        """同步发送 Telegram 消息（纯文本，不使用 parse_mode 避免格式错误）。"""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text,
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError) as exc:
            print(f"⚠️  Telegram 告警发送失败: {exc}")
            return False

    # ── 统一告警入口 ──

    async def send_alert(self, level: str, title: str, message: str, alert_key: str = "") -> bool:
        """异步发送告警（支持去重，Bark 优先，Telegram 备选）。

        Args:
            level: AlertLevel 值（WARNING / CRITICAL / RECOVERY）
            title: 告警标题
            message: 告警详情
            alert_key: 去重 key，空则不去重

        Returns:
            是否实际发送了消息
        """
        if not self.is_configured:
            return False

        if alert_key and not self._should_send(alert_key):
            return False

        # v0.5.1：在 WARNING / CRITICAL 告警正文末尾追加最近失败 trace_id 链接，
        # 便于值班人员一键回溯。RECOVERY 级别不带 trace_id。
        if level != AlertLevel.RECOVERY:
            recent_traces = self.consume_recent_failure_traces()
            if recent_traces:
                trace_lines = "\n".join(f"  · /admin/logs?trace_id={tid}" for tid in recent_traces)
                message = f"{message}\n\n最近 {len(recent_traces)} 个失败 trace_id:\n{trace_lines}"

        success = False
        channel = "none"

        # 优先 Bark
        if self.bark_configured:
            channel = "bark"
            success = await asyncio.to_thread(self._send_bark_sync, title, message, "Laplace", level)

        # Bark 未配置或发送失败时，fallback Telegram
        if not success and self.telegram_configured:
            channel = "telegram"
            text = self._format_telegram_message(level, title, message)
            success = await asyncio.to_thread(self._send_telegram_sync, text)

        # 记录历史
        self._record_history(level, title, message, channel, success)
        return success


# ── 单例 ──

_alerter: Alerter | None = None


def get_alerter() -> Alerter:
    """获取全局 Alerter 单例。"""
    global _alerter
    if _alerter is None:
        _alerter = Alerter()
    return _alerter
