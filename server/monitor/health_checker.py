"""
Laplace — 后台定时模型探活

每 N 秒对所有配置模型发送轻量探活请求，
连续 2 次失败触发 Telegram 告警，恢复时发送恢复通知。
MONITOR_PROBE_INTERVAL=0 完全禁用。
"""

from __future__ import annotations

import asyncio
import os
import time

# 探活默认间隔（秒）
_DEFAULT_PROBE_INTERVAL = 120

# 连续失败多少次才告警
_FAIL_THRESHOLD = 2


class HealthChecker:
    """后台模型探活器。"""

    def __init__(self) -> None:
        self._interval = int(os.getenv("MONITOR_PROBE_INTERVAL", str(_DEFAULT_PROBE_INTERVAL)))
        # 每个模型的连续失败计数
        self._fail_counts: dict[str, int] = {}
        # 已经告警过的模型（避免重复 CRITICAL，由 alerter 去重兜底）
        self._alerted_models: set[str] = set()
        self._task: asyncio.Task | None = None

    @property
    def is_enabled(self) -> bool:
        return self._interval > 0

    async def _probe_model(self, provider_name: str, adapter, model: str) -> bool:
        """对单个模型发送轻量探活请求（纯文本模式，不走 JSON 解析）。"""
        try:
            await adapter.chat_completion(
                model=model,
                system_prompt="",
                user_message="hi",
                max_tokens=1,
                temperature=0.0,
                json_mode=False,
            )
            return True
        except Exception as exc:
            print(f"  ✗ [probe] {provider_name}/{model} 探活失败: {exc}")
            return False

    async def _run_probe_cycle(self) -> None:
        """执行一轮探活，遍历所有 provider 的所有 model。"""
        from server.llm.provider import PROVIDERS
        from server.monitor.alerter import get_alerter
        from server.monitor.metrics import get_collector

        collector = get_collector()
        alerter = get_alerter()

        for provider in PROVIDERS:
            if provider.adapter is None:
                continue
            for model in provider.models:
                is_up = await self._probe_model(provider.name, provider.adapter, model)
                collector.set_model_available(model, is_up)

                if is_up:
                    # 如果之前告警过，发恢复通知
                    if model in self._alerted_models:
                        self._alerted_models.discard(model)
                        await alerter.send_alert(
                            level="INFO",
                            title="模型已恢复",
                            message=f"模型 `{model}` ({provider.name}) 已恢复可用",
                            alert_key=f"model_recover:{model}",
                        )
                    self._fail_counts[model] = 0
                else:
                    self._fail_counts[model] = self._fail_counts.get(model, 0) + 1
                    if self._fail_counts[model] >= _FAIL_THRESHOLD and model not in self._alerted_models:
                        self._alerted_models.add(model)
                        await alerter.send_alert(
                            level="CRITICAL",
                            title="模型不可用",
                            message=f"模型 `{model}` ({provider.name}) 连续 {self._fail_counts[model]} 次探活失败",
                            alert_key=f"model_down:{model}",
                        )

    async def _loop(self) -> None:
        """探活主循环。"""
        # 启动后等一个周期再开始首次探活，避免启动时 LLM 还未就绪
        await asyncio.sleep(min(self._interval, 30))
        print(f"[monitor] 模型探活已启动，间隔 {self._interval}s")
        while True:
            try:
                probe_start = time.monotonic()
                await self._run_probe_cycle()
                elapsed = time.monotonic() - probe_start
                print(f"[monitor] 探活完成，耗时 {elapsed:.1f}s")
            except Exception as exc:
                print(f"[monitor] 探活异常: {exc}")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        """在当前 event loop 中启动后台探活任务。"""
        if not self.is_enabled:
            print("[monitor] 模型探活已禁用 (MONITOR_PROBE_INTERVAL=0)")
            return
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        """停止后台探活任务。"""
        if self._task is not None:
            self._task.cancel()
            self._task = None


# ── 单例 ──

_checker: HealthChecker | None = None


def get_health_checker() -> HealthChecker:
    """获取全局 HealthChecker 单例。"""
    global _checker
    if _checker is None:
        _checker = HealthChecker()
    return _checker


def start_probe_loop() -> None:
    """便捷函数：启动探活循环。"""
    get_health_checker().start()
