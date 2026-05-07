"""
Laplace — Skill Executor

接收路由阶段输出的 SkillCall 列表，按 domain 分组 AND 合并执行。
包含执行阶段的兜底降级逻辑。
"""

from __future__ import annotations

from pydantic import ValidationError

from server.query_executor import load_database
from server.skills.base import SKILL_REGISTRY, QuerySkill, ResponseSkill


class ExecutionResult:
    """Skill 执行结果。"""

    def __init__(
        self,
        servants: list[dict],
        total_found: int,
        response_skill: ResponseSkill | None = None,
        fallback_message: str | None = None,
        is_fallback: bool = False,
    ):
        self.servants = servants
        self.total_found = total_found
        self.response_skill = response_skill
        self.fallback_message = fallback_message
        self.is_fallback = is_fallback


class SkillExecutor:
    """执行 Skill 调用并合并结果。"""

    def execute(
        self,
        skill_calls: list[dict],
        response_skill_name: str = "respond_servant_list",
    ) -> ExecutionResult:
        """执行一组 SkillCall，返回合并结果。

        Args:
            skill_calls: [{"skill_name": str, "params": dict}, ...]
            response_skill_name: 使用的 Response Skill 名称

        Returns:
            ExecutionResult
        """
        db = load_database()

        # 查找 Response Skill
        response_skill = self._resolve_response_skill(response_skill_name)

        # 校验并收集 Query Skills
        query_skills: list[tuple[QuerySkill, dict]] = []
        for call in skill_calls:
            skill_name = call.get("skill_name", "")
            params = call.get("params", {})

            skill = SKILL_REGISTRY.get(skill_name)
            if skill is None:
                return ExecutionResult(
                    servants=[],
                    total_found=0,
                    response_skill=response_skill,
                    fallback_message=f"未知的查询能力：{skill_name}",
                    is_fallback=True,
                )

            if not isinstance(skill, QuerySkill):
                continue  # 跳过非 QuerySkill

            # Pydantic 参数校验
            if skill.params_schema is not None:
                try:
                    validated = skill.params_schema(**params)
                    params = validated.model_dump()
                except (ValidationError, TypeError) as e:
                    return ExecutionResult(
                        servants=[],
                        total_found=0,
                        response_skill=response_skill,
                        fallback_message=f"参数校验失败（{skill_name}）：{e}",
                        is_fallback=True,
                    )

            query_skills.append((skill, params))

        if not query_skills:
            return ExecutionResult(
                servants=[],
                total_found=0,
                response_skill=response_skill,
                fallback_message="没有有效的查询条件，请尝试更具体的描述。",
                is_fallback=True,
            )

        # 按 domain 分组，同 domain AND 合并（一次数据扫描）
        results = self._execute_and_merge(db, query_skills)

        # 按稀有度降序 → collectionNo 升序排序
        results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))

        total_found = len(results)

        # 执行阶段兜底：结果为空
        if total_found == 0:
            return ExecutionResult(
                servants=[],
                total_found=0,
                response_skill=response_skill,
                fallback_message="未找到匹配的从者，你可以尝试调整查询条件。",
                is_fallback=True,
            )

        return ExecutionResult(
            servants=results,
            total_found=total_found,
            response_skill=response_skill,
        )

    def _execute_and_merge(
        self,
        db: list[dict],
        query_skills: list[tuple[QuerySkill, dict]],
    ) -> list[dict]:
        """同 domain Skills AND 合并执行，一次数据扫描。

        对于有自定义 execute() 的 Skill（如 compare_servants），
        单独执行并取交集。
        """
        # 检查是否有自定义 execute 的 Skill（覆盖了默认行为）
        custom_skills = []
        filter_skills = []

        for skill, params in query_skills:
            # 检查子类是否覆盖了 execute 方法
            if type(skill).execute is not QuerySkill.execute:
                custom_skills.append((skill, params))
            else:
                filter_skills.append((skill, params))

        # 对于普通 filter Skills，一次扫描 AND 合并
        if filter_skills:
            results = []
            for servant in db:
                if all(skill.filter(servant, params) for skill, params in filter_skills):
                    results.append(servant)
        else:
            results = list(db)

        # 对于自定义 execute Skills，分别执行后取交集
        for skill, params in custom_skills:
            custom_results = skill.execute(db, params)
            custom_ids = {s["id"] for s in custom_results}
            results = [s for s in results if s["id"] in custom_ids]

        return results

    def _resolve_response_skill(self, name: str) -> ResponseSkill | None:
        """解析 Response Skill。"""
        skill = SKILL_REGISTRY.get(name)
        if skill is not None and isinstance(skill, ResponseSkill):
            return skill
        # fallback: 尝试默认
        default = SKILL_REGISTRY.get("respond_servant_list")
        if default is not None and isinstance(default, ResponseSkill):
            return default
        return None
