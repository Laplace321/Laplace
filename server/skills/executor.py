"""
Laplace — Skill Executor

接收路由阶段输出的 SkillCall 列表，按 domain 分组 AND 合并执行。
包含执行阶段的兜底降级逻辑。
"""

from __future__ import annotations

import time

from pydantic import ValidationError

from server.query_executor import load_ce_database, load_database
from server.skills.base import SKILL_REGISTRY, QuerySkill, ResponseSkill

# ── 执行层 Clarification 类型常量 ──
CLARIFICATION_MULTI_CANDIDATE = "multi_candidate"
CLARIFICATION_EMPTY_NAME = "empty_result_name"
CLARIFICATION_EMPTY_FILTER = "empty_result_filter"


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
        clarification: dict | None = None,
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
        self.clarification = clarification


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

        # 按 domain 分组
        domain_skills = [(s, p) for s, p in query_skills if s.domain == "servant"]
        ce_skills = [(s, p) for s, p in query_skills if s.domain == "ce"]
        knowledge_skills = [(s, p) for s, p in query_skills if s.domain not in ("servant", "ce")]

        # ── CE domain 独立执行路径 ──
        if ce_skills:
            ce_db = load_ce_database()
            ce_results = self._execute_and_merge(ce_db, ce_skills)
            ce_results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))
            total_found = len(ce_results)
            elapsed_ms = (time.monotonic() - start_time) * 1000

            # 多候选检测（ce_lookup 单名称查询且 >1 结果）
            if self._is_single_name_lookup(accepted, domain="ce") and total_found > 1:
                clarification = self._build_multi_candidate_clarification(ce_results, accepted, domain="ce")
                if clarification:
                    return ExecutionResult(
                        servants=ce_results,
                        total_found=total_found,
                        response_skill=response_skill,
                        accepted_skills=accepted,
                        rejected_skills=rejected,
                        execution_time_ms=elapsed_ms,
                        clarification=clarification,
                    )

            if total_found == 0:
                # 空结果 clarification 引导
                clarification = self._build_empty_result_clarification(accepted, domain="ce")
                if clarification:
                    return ExecutionResult(
                        servants=[],
                        total_found=0,
                        response_skill=response_skill,
                        accepted_skills=accepted,
                        rejected_skills=rejected,
                        execution_time_ms=elapsed_ms,
                        clarification=clarification,
                    )
                return ExecutionResult(
                    servants=[],
                    total_found=0,
                    response_skill=response_skill,
                    fallback_message="未找到匹配的概念礼装，你可以尝试调整查询条件。",
                    is_fallback=True,
                    accepted_skills=accepted,
                    rejected_skills=rejected,
                    execution_time_ms=elapsed_ms,
                )

            return ExecutionResult(
                servants=ce_results,
                total_found=total_found,
                response_skill=response_skill,
                accepted_skills=accepted,
                rejected_skills=rejected,
                execution_time_ms=elapsed_ms,
            )

        # ── Knowledge domain（coronation 等）执行路径 ──
        custom_context: list[dict] = []
        knowledge_servants: list[dict] = []

        for skill, params in knowledge_skills:
            ctx_entries = skill.execute(db, params)
            custom_context.extend(ctx_entries)
            # 从配队数据中提取关联从者（通过 collectionNo 匹配 db）
            knowledge_servants.extend(self._extract_servants_from_context(db, ctx_entries))

        # ── Servant domain 执行路径 ──
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

        # ── 多候选检测（lookup_servant / compare_servants 单名称查询且 >1 结果） ──
        if self._is_single_name_lookup(accepted, domain="servant") and total_found > 1:
            clarification = self._build_multi_candidate_clarification(results, accepted, domain="servant")
            if clarification:
                return ExecutionResult(
                    servants=results,
                    total_found=total_found,
                    response_skill=response_skill,
                    accepted_skills=accepted,
                    rejected_skills=rejected,
                    execution_time_ms=elapsed_ms,
                    clarification=clarification,
                )

        # 执行阶段兜底：结果为空
        if total_found == 0:
            # Skill Fallback: lookup_servant 空结果 → resolve_nickname（同步缓存路径）
            if self._should_try_nickname_resolve(accepted):
                resolve_result = self._try_resolve_nickname(db, accepted, response_skill, rejected, start_time)
                if resolve_result:
                    return resolve_result

            # 空结果 clarification 引导（筛选类→放宽条件，名称类→等待异步 LLM 猜测）
            clarification = self._build_empty_result_clarification(accepted, domain="servant")
            if clarification:
                return ExecutionResult(
                    servants=[],
                    total_found=0,
                    response_skill=response_skill,
                    accepted_skills=accepted,
                    rejected_skills=rejected,
                    execution_time_ms=elapsed_ms,
                    clarification=clarification,
                )

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

    # ── 执行层 Clarification 辅助方法 ──

    def _is_single_name_lookup(self, accepted_skills: list[dict], *, domain: str = "servant") -> bool:
        """判断是否为单名称查询场景（可能触发多候选 clarification）。

        触发条件：accepted_skills 中仅有一个名称查询 Skill（不与其他筛选条件组合）。
        """
        if len(accepted_skills) != 1:
            return False
        skill_name = accepted_skills[0].get("skill_name", "")
        if domain == "servant":
            return skill_name in ("lookup_servant", "compare_servants")
        if domain == "ce":
            return skill_name == "ce_lookup"
        return False

    def _build_multi_candidate_clarification(
        self,
        results: list[dict],
        accepted_skills: list[dict],
        *,
        domain: str = "servant",
    ) -> dict | None:
        """从匹配结果构建多候选 clarification 选项。

        返回 ClarificationRequest 兼容的 dict，或 None（不需要 clarification）。
        """
        if len(results) <= 1:
            return None

        from server.translation import get_class_map

        class_map = get_class_map()

        # 提取查询名称
        query_name = ""
        if accepted_skills:
            params = accepted_skills[0].get("params", {})
            query_name = params.get("name", "") or ""
            if not query_name and "names" in params:
                # compare_servants: 暂不对多 name 触发（复杂度高）
                return None

        if domain == "servant":
            options = []
            for servant in results[:8]:  # 最多 8 个选项
                cn_name = servant.get("aliasCN", "")
                rarity = servant.get("rarity", 0)
                class_name = servant.get("className", "")
                class_cn = class_map.get(class_name.lower(), class_name)
                collection_no = servant.get("collectionNo", 0)
                label = f"{'★' * rarity} {cn_name}（{class_cn}）" if class_cn else f"{'★' * rarity} {cn_name}"
                options.append({"id": str(collection_no), "label": label})
        elif domain == "ce":
            options = []
            for ce_item in results[:8]:
                ce_name_cn = ce_item.get("nameCn", "") or ce_item.get("name", "")
                rarity = ce_item.get("rarity", 0)
                collection_no = ce_item.get("collectionNo", 0)
                label = f"{'★' * rarity} {ce_name_cn}"
                options.append({"id": str(collection_no), "label": label})
        else:
            return None

        if len(options) <= 1:
            return None

        return {
            "type": CLARIFICATION_MULTI_CANDIDATE,
            "question": f"「{query_name}」匹配到多个结果，请选择你要查询的：",
            "options": options,
            "ambiguous_field": "name",
        }

    def _build_empty_result_clarification(
        self,
        accepted_skills: list[dict],
        *,
        domain: str = "servant",
    ) -> dict | None:
        """根据 Skill 类型构建空结果 clarification 引导。

        名称查询空结果 → 标记为 empty_result_name（pipeline 层异步 LLM 猜测）
        筛选查询空结果 → 纯规则生成放宽条件选项
        """
        if not accepted_skills:
            return None

        # 判断是否为名称查询
        name_skills = {"lookup_servant", "ce_lookup", "compare_servants"}
        is_name_query = any(s.get("skill_name") in name_skills for s in accepted_skills)

        if is_name_query:
            # 名称查询空结果：标记为需要 LLM 猜测（由 pipeline 层异步处理）
            query_name = ""
            for skill_data in accepted_skills:
                params = skill_data.get("params", {})
                query_name = params.get("name", "") or ""
                if query_name:
                    break
                names = params.get("names", [])
                if names:
                    query_name = "、".join(names)
                    break

            return {
                "type": CLARIFICATION_EMPTY_NAME,
                "question": f"未找到「{query_name}」，正在智能识别...",
                "options": [],  # 由 pipeline 层异步填充
                "ambiguous_field": "name",
                "query_name": query_name,  # 供 pipeline 层 LLM 猜测使用
                "domain": domain,
            }

        # 筛选查询空结果：生成放宽条件选项
        return self._build_filter_relaxation_clarification(accepted_skills, domain=domain)

    def _build_filter_relaxation_clarification(
        self,
        accepted_skills: list[dict],
        *,
        domain: str = "servant",
    ) -> dict | None:
        """分析当前筛选条件，生成放宽建议选项。纯规则逻辑，不需要 LLM。"""
        from server.translation import describe_filters

        options: list[dict] = []
        condition_descriptions = describe_filters(accepted_skills)
        conditions_text = "、".join(f"「{d}」" for d in condition_descriptions)

        for skill_data in accepted_skills:
            skill_name = skill_data.get("skill_name", "")
            params = skill_data.get("params", {})

            # 稀有度限制
            if skill_name in ("search_by_rarity", "ce_search_by_rarity"):
                val = params.get("value", "")
                options.append({"id": f"drop:{skill_name}", "label": f"去掉{val}星限制"})

            # 职阶限制
            elif skill_name == "search_by_class":
                class_name = params.get("className", "")
                options.append({"id": f"drop:{skill_name}", "label": f"去掉{class_name}职阶限制"})

            # 效果数值条件
            elif skill_name in (
                "search_by_effect",
                "search_by_skill_effect",
                "search_by_np_effect",
                "ce_search_by_effect",
            ):
                if params.get("minValue") or params.get("min_value"):
                    options.append({"id": f"drop_min:{skill_name}", "label": "去掉数值下限"})
                if params.get("targetType") or params.get("target_type"):
                    options.append({"id": f"drop_target:{skill_name}", "label": "不限目标类型"})

            # 礼装获取方式
            elif skill_name == "ce_search_by_obtain":
                options.append({"id": f"drop:{skill_name}", "label": "不限获取方式"})

            # 礼装攻击类型
            elif skill_name == "ce_search_by_atk_type":
                options.append({"id": f"drop:{skill_name}", "label": "不限攻击类型"})

        if not options:
            return None

        entity_label = "从者" if domain == "servant" else "概念礼装"
        question = f"未找到同时满足{conditions_text}的{entity_label}，你可以放宽条件："

        return {
            "type": CLARIFICATION_EMPTY_FILTER,
            "question": question,
            "options": options,
            "ambiguous_field": "filter_relaxation",
        }

    async def guess_candidates_async(
        self,
        result: ExecutionResult,
    ) -> ExecutionResult:
        """异步 LLM 猜测候选（名称查询空结果时调用）。

        当 ExecutionResult.clarification.type == CLARIFICATION_EMPTY_NAME 时，
        由 pipeline 层调用此方法进行 LLM 猜测并填充候选选项。
        同时兼容原有的 resolve_nickname 异步路径。
        """
        clarification = result.clarification
        if not clarification or clarification.get("type") != CLARIFICATION_EMPTY_NAME:
            # 非名称空结果场景：尝试旧的 resolve_nickname 异步路径
            if result.is_fallback and self._should_try_nickname_resolve(result.accepted_skills):
                return await self._try_resolve_nickname_async_internal(result)
            return result

        query_name = clarification.get("query_name", "")
        domain = clarification.get("domain", "servant")
        if not query_name:
            return result

        # 调用 LLM 猜测候选
        resolve_skill = SKILL_REGISTRY.get("resolve_nickname")
        if resolve_skill is None or not hasattr(resolve_skill, "execute_async"):
            return result

        db = load_database() if domain == "servant" else load_ce_database()
        resolve_params = {"name": query_name}
        candidates = await resolve_skill.execute_async(db, resolve_params)

        if not candidates:
            # LLM 猜测失败，保留 clarification 但标记为 fallback
            result.is_fallback = True
            result.fallback_message = f"未找到「{query_name}」相关的结果，请尝试更具体的名称。"
            result.clarification = None
            return result

        if len(candidates) == 1:
            # 唯一候选：直接返回结果，不触发 clarification
            candidates.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))
            return ExecutionResult(
                servants=candidates,
                total_found=len(candidates),
                response_skill=result.response_skill,
                accepted_skills=result.accepted_skills + [{"skill_name": "resolve_nickname", "params": resolve_params}],
                rejected_skills=result.rejected_skills,
                execution_time_ms=result.execution_time_ms,
            )

        # 多候选：构建 clarification 选项
        multi_clarification = self._build_multi_candidate_clarification(
            candidates, result.accepted_skills, domain=domain
        )
        if multi_clarification:
            multi_clarification["question"] = f"「{query_name}」可能是以下之一，请选择："
            result.clarification = multi_clarification
            result.servants = candidates
            result.total_found = len(candidates)
        else:
            # 构建失败，直接返回所有候选
            result.servants = candidates
            result.total_found = len(candidates)
            result.clarification = None

        return result

    async def _try_resolve_nickname_async_internal(self, result: ExecutionResult) -> ExecutionResult:
        """内部异步昵称识别（从 try_resolve_nickname_async 提取）。"""
        resolve_skill = SKILL_REGISTRY.get("resolve_nickname")
        if resolve_skill is None or not hasattr(resolve_skill, "execute_async"):
            return result

        name_param = result.accepted_skills[0].get("params", {}).get("name", "")
        if not name_param:
            return result

        db = load_database()
        resolve_params = {"name": name_param}
        results = await resolve_skill.execute_async(db, resolve_params)

        if not results:
            return result

        results.sort(key=lambda x: (-x.get("rarity", 0), x.get("collectionNo", 0)))

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
