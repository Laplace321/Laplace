"""
Laplace — Skills Package

自动导入所有 query/ 和 response/ 模块，触发 @register_skill 注册。
"""

import importlib
import pkgutil
from pathlib import Path


def _auto_import_submodules(package_path: str, package_name: str) -> None:
    """自动导入指定包路径下的所有模块。"""
    for _importer, module_name, _is_pkg in pkgutil.iter_modules([package_path]):
        importlib.import_module(f"{package_name}.{module_name}")


_skills_dir = Path(__file__).parent

# 自动导入 query/ 下所有模块
_query_dir = _skills_dir / "query"
if _query_dir.exists():
    _auto_import_submodules(str(_query_dir), "server.skills.query")

# 自动导入 response/ 下所有模块
_response_dir = _skills_dir / "response"
if _response_dir.exists():
    _auto_import_submodules(str(_response_dir), "server.skills.response")
