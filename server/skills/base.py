"""
Laplace — Skill Base Classes

Skill 框架的核心基类和注册表。
每个 Skill 通过 @register_skill 装饰器自动注册到 SKILL_REGISTRY。
"""

from __future__ import annotations

from abc import abstractmethod

from pydantic import BaseModel

# 全局 Skill 注册表
SKILL_REGISTRY: dict[str, BaseSkill] = {}


def register_skill(cls: type[BaseSkill]) -> type[BaseSkill]:
    """装饰器：实例化 Skill 并注册到 SKILL_REGISTRY。"""
    instance = cls()
    SKILL_REGISTRY[instance.name] = instance
    return cls


class BaseSkill:
    """Skill 基类。"""

    name: str = ""
    description: str = ""

    @property
    def prompt_fragment(self) -> str:
        """供 Stage 2 参数填充 Prompt 使用的描述片段。"""
        return ""

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        """Few-shot 示例列表。"""
        return []


class QuerySkill(BaseSkill):
    """查询类 Skill 基类。"""

    domain: str = "servant"

    @property
    def params_schema(self) -> type[BaseModel] | None:
        """参数 Pydantic 模型（可选）。"""
        return None

    def filter(self, servant: dict, params: dict) -> bool:
        """判断单个从者是否匹配。默认实现返回 True。"""
        return True

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """执行查询。默认实现为遍历 db 并调用 filter。"""
        return [s for s in db if self.filter(s, params)]


class ResponseSkill(BaseSkill):
    """回复类 Skill 基类。"""

    @abstractmethod
    def build_prompt(self, user_message: str, context_json: str) -> str:
        """构建 RAG 生成 Prompt。"""
        ...
