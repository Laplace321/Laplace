"""Backward-compatible re-exports. 新代码请直接 import server.llm。"""

from server.llm import (  # noqa: F401
    PROVIDERS,
    LLMProvider,
    agent_completion,
    chat_completion,
    extract_json_object,
)
from server.llm.base import LLMResponseFormatUnsupported  # noqa: F401
from server.llm.provider import _load_providers  # noqa: F401
