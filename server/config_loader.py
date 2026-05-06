"""
Laplace — Config Loader

基于文件 mtime 的配置缓存，请求时懒检查，文件变更自动重载。
用于 server/config/ 下的可运营配置（translations.json、nicknames.json 等）。
"""

import json
from pathlib import Path


class CachedConfig:
    """基于 mtime 的配置缓存，请求时懒检查，文件变更自动重载。

    每次 get() 仅执行一次 stat() 系统调用（微秒级），性能开销可忽略。
    """

    def __init__(self, path: Path):
        self._path = path
        self._data: dict | None = None
        self._mtime: float = 0.0

    def get(self) -> dict:
        """获取配置数据，文件变更时自动重载。"""
        try:
            current_mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            return self._data or {}
        if self._data is None or current_mtime > self._mtime:
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            self._mtime = current_mtime
        return self._data
