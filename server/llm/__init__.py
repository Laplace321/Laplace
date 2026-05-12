"""
Laplace — LLM 模块公共 API

所有下游代码统一通过此模块导入 LLM 能力：
    from server.llm import chat_completion, agent_completion, extract_json_object
"""

from server.llm.base import extract_json_object  # noqa: F401
from server.llm.provider import (  # noqa: F401
    PROVIDERS,
    LLMProvider,
    agent_completion,
    chat_completion,
)
