"""
Laplace — Skill Base Classes & Registry

定义 Skill 基类和全局注册表。
每个 Skill 是一个独立的查询或回复能力单元。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel

# 全局 Skill 注册表
SKILL_REGISTRY: dict[str, BaseSkill] = {}


def register_skill(cls: type[BaseSkill]) -> type[BaseSkill]:
    """类装饰器：将 Skill 类实例注册到 SKILL_REGISTRY。"""
    instance = cls()
    SKILL_REGISTRY[instance.name] = instance
    return cls


class BaseSkill(ABC):
    """所有 Skill 的抽象基类。"""

    # 子类必须定义
    name: str = ""
    description: str = ""  # 一句话描述，供 Stage 1 路由 Prompt 使用
    domain: str = "servant"  # 数据域标记（当前只有 servant）

    @property
    def params_schema(self) -> type[BaseModel] | None:
        """返回该 Skill 的参数 Pydantic Model（可选）。"""
        return None

    @property
    def prompt_fragment(self) -> str:
        """返回 Stage 2 参数填充的专属 Prompt 片段。"""
        return ""

    @property
    def few_shot_examples(self) -> list[dict[str, str]]:
        """返回 few-shot 示例列表，每个元素 {"input": ..., "output": ...}。"""
        return []


class QuerySkill(BaseSkill):
    """Query Skill 基类：纯过滤，接收从者列表并返回过滤后的子集。"""

    @abstractmethod
    def filter(self, servant: dict, params: dict) -> bool:
        """判断单个从者是否匹配该 Skill 的过滤条件。

        Args:
            servant: 从者数据 dict
            params: 该 Skill 的参数 dict（已经过 Pydantic 校验）

        Returns:
            True 表示匹配，False 表示不匹配
        """
        ...

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """在整个数据库上执行过滤。

        子类可覆盖此方法实现特殊逻辑（如 compare_servants 的多名称查询）。
        默认实现：遍历 db 逐条调用 filter()。
        """
        return [s for s in db if self.filter(s, params)]


class ResponseSkill(BaseSkill):
    """Response Skill 基类：生成回复模板。"""

    @property
    @abstractmethod
    def generation_prompt(self) -> str:
        """返回该 Response Skill 的 generation_prompt 模板。

        模板中可使用 {user_query} 和 {context_json} 占位符。
        """
        ...

    def build_prompt(self, user_query: str, context_json: str) -> str:
        """构建最终的 generation prompt。"""
        return self.generation_prompt.format(
            user_query=user_query,
            context_json=context_json,
        )

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """Response Skill 不执行过滤，直接返回空列表。"""
        return db
