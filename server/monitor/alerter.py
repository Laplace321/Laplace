"""
Laplace — Telegram Bot 告警推送

使用 stdlib urllib（无新增依赖），支持告警去重。
TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未配置时静默跳过。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# 北京时间
_BEIJING_TZ = timezone(timedelta(hours=8))

# 告警去重窗口（秒）
_DEDUP_WINDOW_SECONDS = 30 * 60  # 30 分钟


class Alerter:
    """Telegram Bot 告警推送器。"""

    def __init__(self) -> None:
        self._bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        # 去重记录：alert_key -> last_sent_timestamp
        self._sent_alerts: dict[str, float] = {}

    @property
    def is_configured(self) -> bool:
        return bool(self._bot_token and self._chat_id)

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

    def _format_message(self, level: str, title: str, message: str) -> str:
        """格式化 Telegram 消息（Markdown）。"""
        now_str = datetime.now(_BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
        level_emoji = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🟢"}.get(level, "⚪")
        return f"{level_emoji} *{level}* | {title}\n\n{message}\n\n_Laplace Monitor · {now_str}_"

    def _send_sync(self, text: str) -> bool:
        """同步发送 Telegram 消息。"""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError) as exc:
            print(f"⚠️  Telegram 告警发送失败: {exc}")
            return False

    async def send_alert(self, level: str, title: str, message: str, alert_key: str = "") -> bool:
        """异步发送告警（支持去重）。

        Args:
            level: CRITICAL / WARNING / INFO
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

        text = self._format_message(level, title, message)
        return await asyncio.to_thread(self._send_sync, text)


# ── 单例 ──

_alerter: Alerter | None = None


def get_alerter() -> Alerter:
    """获取全局 Alerter 单例。"""
    global _alerter
    if _alerter is None:
        _alerter = Alerter()
    return _alerter
