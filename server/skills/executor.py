"""
Laplace — Skill Executor

接收路由阶段输出的 SkillCall 列表，按 domain 分组 AND 合并执行。
包含执行阶段的兜底降级逻辑。
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from server.query_executor import load_database
from server.skills.base import SKILL_REGISTRY, QuerySkill, ResponseSkill


class ExecutionResult:
    """Skill 执行结果（含诊断信息）。"""

    def __init__(
        self,
        servants: list[dict],
        total_found: int,
        response_skill: ResponseSkill | None = None,
        fallback_message: str | None = None,
        is_fallback: bool = False,
        accepted_skills: list[dict] | None = None,
        rejected_skills: list[dict] | None = None,
        execution_time_ms: float = 0.0,
        custom_context: list[dict] | None = None,
    ):
        self.servants = servants
        self.total_found = total_found
        self.response_skill = response_skill
        self.fallback_message = fallback_message
        self.is_fallback = is_fallback
        self.accepted_skills = accepted_skills or []
        self.rejected_skills = rejected_skills or []
        self.execution_time_ms = execution_time_ms
        self.custom_context = custom_context


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
            ExecutionResult（含 accepted_skills / rejected_skills 诊断信息）
        """
        start_time = time.monotonic()
        db = load_database()

        # 查找 Response Skill
        response_skill = self._resolve_response_skill(response_skill_name)

        # 校验并收集 Query Skills
        query_skills: list[tuple[QuerySkill, dict]] = []
        accepted: list[dict] = []
        rejected: list[dict] = []

        for call in skill_calls:
            skill_name = call.get("skill_name", "")
            params = call.get("params", {})

            skill = SKILL_REGISTRY.get(skill_name)
            if skill is None or not isinstance(skill, QuerySkill):
                rejected.append(
                    {
                        "skill_name": skill_name,
                        "reason": "not_found",
                        "detail": f"不在 SKILL_REGISTRY 中（已注册: {list(SKILL_REGISTRY.keys())}）",
                    }
                )
                continue

            # Pydantic 参数校验（容错：校验失败跳过该 Skill）
            if skill.params_schema is not None:
                try:
                    validated = skill.params_schema(**params)
                    params = validated.model_dump(by_alias=False)
                except (ValidationError, TypeError) as e:
                    rejected.append(
                        {
                            "skill_name": skill_name,
                            "reason": "validation_error",
                            "detail": str(e),
                        }
                    )
                    continue

            query_skills.append((skill, params))
            accepted.append({"skill_name": skill_name, "params": params})

        elapsed_ms = (time.monotonic() - start_time) * 1000

        if not query_skills:
            return ExecutionResult(
                servants=[],
                total_found=0,
                response_skill=response_skill,
                fallback_message="没有有效的查询条件，请尝试更具体的描述。",
                is_fallback=True,
                accepted_skills=accepted,
                rejected_skills=rejected,
                execution_time_ms=elapsed_ms,
            )

        # 分离非 servant domain 的 Skills（如 coronation），它们返回知识/配队数据而非从者列表
        domain_skills = [(s, p) for s, p in query_skills if s.domain == "servant"]
        knowledge_skills = [(s, p) for s, p in query_skills if s.domain != "servant"]

        # 执行 knowledge_skills，收集 custom_context 和关联从者
        custom_context: list[dict] = []
        knowledge_servants: list[dict] = []

        for skill, params in knowledge_skills:
            ctx_entries = skill.execute(db, params)
            custom_context.extend(ctx_entries)
            # 从配队数据中提取关联从者（通过 collectionNo 匹配 db）
            knowledge_servants.extend(self._extract_servants_from_context(db, ctx_entries))

        # 按 domain 分组，同 domain AND 合并（一次数据扫描）
        if domain_skills:
            results = self._execute_and_merge(db, domain_skills)
        elif knowledge_servants:
            # 纯 knowledge Skills 但有关联从者
            results = knowledge_servants
        else:
            results = []

        # 如果有 knowledge_skills 产出的从者，合并（去重）
        if domain_skills and knowledge_servants:
            existing_ids = {s.get("id") for s in results}
            for s in knowledge_servants:
                if s.get("id") not in existing_ids:
                    results.append(s)
                    existing_ids.add(s.get("id"))

        # 按稀有度降序 → collectionNo 升序排序
        results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))

        total_found = len(results)
        elapsed_ms = (time.monotonic() - start_time) * 1000

        # 如果有 custom_context，即使 servants 为空也不进 fallback
        if custom_context:
            return ExecutionResult(
                servants=results,
                total_found=total_found,
                response_skill=response_skill,
                accepted_skills=accepted,
                rejected_skills=rejected,
                execution_time_ms=elapsed_ms,
                custom_context=custom_context,
            )

        # 执行阶段兜底：结果为空
        if total_found == 0:
            # Skill Fallback: lookup_servant 空结果 → resolve_nickname
            if self._should_try_nickname_resolve(accepted):
                resolve_result = self._try_resolve_nickname(db, accepted, response_skill, rejected, start_time)
                if resolve_result:
                    return resolve_result

            return ExecutionResult(
                servants=[],
                total_found=0,
                response_skill=response_skill,
                fallback_message="未找到匹配的从者，你可以尝试调整查询条件。",
                is_fallback=True,
                accepted_skills=accepted,
                rejected_skills=rejected,
                execution_time_ms=elapsed_ms,
            )

        return ExecutionResult(
            servants=results,
            total_found=total_found,
            response_skill=response_skill,
            accepted_skills=accepted,
            rejected_skills=rejected,
            execution_time_ms=elapsed_ms,
        )

    def _extract_servants_from_context(self, db: list[dict], ctx_entries: list[dict]) -> list[dict]:
        """从 custom_context 条目中提取关联从者（通过 collectionNo 匹配 db）。"""
        collection_nos: set[int] = set()
        for entry in ctx_entries:
            if entry.get("type") != "team":
                continue
            for rc in entry.get("roleCategories", []):
                for s in rc.get("servants", []):
                    cno = s.get("collectionNo")
                    if cno is not None:
                        collection_nos.add(cno)

        if not collection_nos:
            return []

        # 从 db 中查找匹配的从者
        return [s for s in db if s.get("collectionNo") in collection_nos]

    def _execute_and_merge(
        self,
        db: list[dict],
        query_skills: list[tuple[QuerySkill, dict]],
    ) -> list[dict]:
        """同 domain Skills AND 合并执行，一次数据扫描。"""
        # 分离自定义 execute 的 Skill 和普通 filter Skill
        custom_skills = []
        filter_skills = []

        for skill, params in query_skills:
            if type(skill).execute is not QuerySkill.execute:
                custom_skills.append((skill, params))
            else:
                filter_skills.append((skill, params))

        # 普通 filter Skills：一次扫描 AND 合并
        if filter_skills:
            results = [
                servant for servant in db if all(skill.filter(servant, params) for skill, params in filter_skills)
            ]
        else:
            results = list(db)

        # 自定义 execute Skills：分别执行后取交集
        for skill, params in custom_skills:
            custom_results = skill.execute(db, params)
            custom_ids = {s["id"] for s in custom_results}
            results = [s for s in results if s["id"] in custom_ids]

        return results

    def _should_try_nickname_resolve(self, accepted_skills: list[dict]) -> bool:
        """判断是否应该触发 resolve_nickname fallback。

        触发条件：
        1. accepted_skills 中仅包含 lookup_servant（纯名称查询）
        2. resolve_nickname 已注册在 SKILL_REGISTRY 中
        """
        if len(accepted_skills) != 1:
            return False
        if accepted_skills[0].get("skill_name") != "lookup_servant":
            return False
        return "resolve_nickname" in SKILL_REGISTRY

    def _try_resolve_nickname(
        self,
        db: list[dict],
        accepted_skills: list[dict],
        response_skill: ResponseSkill | None,
        rejected_skills: list[dict],
        start_time: float,
    ) -> ExecutionResult | None:
        """尝试通过 LLM 昵称识别进行 fallback（同步路径，仅缓存命中有效）。

        返回 ExecutionResult（成功时）或 None（识别失败时）。
        """
        resolve_skill = SKILL_REGISTRY.get("resolve_nickname")
        if resolve_skill is None or not isinstance(resolve_skill, QuerySkill):
            return None

        # 提取 lookup_servant 的 name 参数
        name_param = accepted_skills[0].get("params", {}).get("name", "")
        if not name_param:
            return None

        # 调用 resolve_nickname（同步路径仅检查缓存）
        resolve_params = {"name": name_param}
        results = resolve_skill.execute(db, resolve_params)

        if not results:
            return None

        # 按稀有度降序 → collectionNo 升序排序
        results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))

        elapsed_ms = (time.monotonic() - start_time) * 1000

        # 追加 resolve_nickname 到 accepted_skills 记录
        accepted_with_resolve = list(accepted_skills) + [{"skill_name": "resolve_nickname", "params": resolve_params}]

        return ExecutionResult(
            servants=results,
            total_found=len(results),
            response_skill=response_skill,
            accepted_skills=accepted_with_resolve,
            rejected_skills=rejected_skills,
            execution_time_ms=elapsed_ms,
        )

    async def try_resolve_nickname_async(
        self,
        result: ExecutionResult,
        skill_calls: list[dict],
    ) -> ExecutionResult:
        """异步昵称识别 fallback（供 main.py 在 async 路由中调用）。

        当 SkillExecutor.execute() 返回 is_fallback=True 且满足触发条件时，
        由 main.py 的 async 路由调用此方法进行 LLM 昵称识别。

        Args:
            result: execute() 返回的原始 fallback 结果
            skill_calls: 原始 skill_calls 列表

        Returns:
            识别成功时返回新的 ExecutionResult，失败时返回原 result 不变
        """
        if not self._should_try_nickname_resolve(result.accepted_skills):
            return result

        resolve_skill = SKILL_REGISTRY.get("resolve_nickname")
        if resolve_skill is None:
            return result

        # 提取 lookup_servant 的 name 参数
        name_param = result.accepted_skills[0].get("params", {}).get("name", "")
        if not name_param:
            return result

        # 异步调用 resolve_nickname
        resolve_params = {"name": name_param}
        if hasattr(resolve_skill, "execute_async"):
            db = load_database()
            results = await resolve_skill.execute_async(db, resolve_params)
        else:
            return result

        if not results:
            return result

        # 按稀有度降序 → collectionNo 升序排序
        results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))

        # 追加 resolve_nickname 到 accepted_skills 记录
        accepted_with_resolve = list(result.accepted_skills) + [
            {"skill_name": "resolve_nickname", "params": resolve_params}
        ]

        return ExecutionResult(
            servants=results,
            total_found=len(results),
            response_skill=result.response_skill,
            accepted_skills=accepted_with_resolve,
            rejected_skills=result.rejected_skills,
            execution_time_ms=result.execution_time_ms,
        )

    def _resolve_response_skill(self, name: str) -> ResponseSkill | None:
        """解析 Response Skill。"""
        skill = SKILL_REGISTRY.get(name)
        if skill is not None and isinstance(skill, ResponseSkill):
            return skill
        default = SKILL_REGISTRY.get("respond_servant_list")
        if default is not None and isinstance(default, ResponseSkill):
            return default
        return None
