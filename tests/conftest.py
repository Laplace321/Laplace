import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 循环导入守门（hotfix v0.4.5）：
# 在任何测试之前以 server.translation 为入口完成一次完整导入。
# translation 模块同时依赖 server.skills.base，若任何 query skill 顶层
# 误写为 ``from server.translation import ...``，会与 translation 本身
# 的导入链构成循环依赖，由本行在 collect 阶段直接暴露为 ImportError。
import server.translation  # noqa: E402, F401
