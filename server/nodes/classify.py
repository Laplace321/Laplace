"""Pipeline A 入口节点 — Stage 0 链路分类器（ADR-024 / ADR-028）。

迁移自 ``server/pipeline.py`` 的 Stage 0 分类逻辑。本节点行为与原代码完全等价：
- 调用 ``build_classifier_prompt`` 提示词，最多 2 次 LLM 重试
- 把分类结果写回 state.classified_pipeline / classifier_confidence / classifier_model
- 累计 trace_total_tokens

降级行为：
- 2 次重试均失败 → 视为 A 链路 + 1.0 置信度（兼容旧逻辑），由 ``edges.after_classify`` 决定后续路径

Task 4 Batch B 多轮对话扩展：
- 入口加载 ``state.extras["session_store"]`` 中的上一轮 TurnSnapshot，注入 prev_summary
- 解析 LLM 输出的 ``turn_type``，写入 ``state.turn_type``
- ``turn_type == "MAJOR"`` 时清空 session（清除上一轮 turn 历史 + pending），避免标记位残留（ADR-026 教训）
- ``state.session_id`` 为空时跳过多轮逻辑，行为与单轮完全一致
"""

from __future__ import annotations

from server.graph.decorators import with_trace
from server.graph.session import SessionStore
from server.graph.state import PipelineState
from server.llm import chat_completion
from server.logger import Phase, log_trace_event
from server.prompts import build_classifier_prompt
from server.schemas import classifier_response_json_schema, parse_classifier_response

# FALLBACK 后置防误判：含任一信号则视为 FGO 查询，强制改回 A 链路
# 关键词覆盖：职阶、星级、FGO 专有名词、常见效果、数值条件
# 注意：从者名/特性等长词典放在 servants_db 与 traits 表里，此处只做"廉价兜底"
_FGO_SIGNAL_KEYWORDS: tuple[str, ...] = (
    # 职阶
    "剑阶",
    "弓阶",
    "枪阶",
    "骑阶",
    "术阶",
    "杀阶",
    "狂阶",
    "saber",
    "archer",
    "lancer",
    "rider",
    "caster",
    "assassin",
    "berserker",
    "ruler",
    "avenger",
    "alterego",
    "moon",
    "foreigner",
    "pretender",
    "shielder",
    "裁定者",
    "复仇者",
    "月癌",
    "降临者",
    "盾兵",
    # FGO 专有名词
    "宝具",
    "技能",
    "羁绊",
    "灵基",
    "礼装",
    "概念礼装",
    "卡池",
    "复刻",
    "活动",
    "戴冠战",
    "冠位",
    "高难",
    "主线",
    "特异点",
    "章节",
    "周年",
    "联动",
    "up",
    # 效果与机制
    "充能",
    "无敌",
    "闪避",
    "暴击",
    "出星",
    "特攻",
    "增伤",
    "buff",
    "debuff",
    "魔放",
    "攻击力",
    "防御力",
    "宝具威力",
    "暴击威力",
    "嘲讽",
    "回避",
    "无敌贯通",
    "a类",
    "b类",
    "c类",
    "d类",
    "乘区",
    "伤害公式",
    # 数值/筛选词
    "推荐",
    "查一下",
    "对比",
    "有哪些",
    "筛选",
    "查询",
    "克制",
    "5星",
    "4星",
    "3星",
    "五星",
    "四星",
    "三星",
    "%以上",
    "%up",
    "np",
    "atk",
    "hp",
)


def _has_fgo_query_signal(message: str) -> bool:
    """检测用户输入是否包含 FGO 查询信号，用于 FALLBACK 后置防误判。

    任一关键词命中即返回 True，调用方应把 pipeline 强制改回 A。
    匹配为大小写不敏感子串匹配，覆盖中英文常见信号。
    """
    if not message:
        return False
    lower = message.lower()
    return any(kw in lower for kw in _FGO_SIGNAL_KEYWORDS)


@with_trace(Phase.NODE_CLASSIFY)
async def classify_node(state: PipelineState) -> PipelineState:
    """Stage 0：链路分类（A=Skill 查询 / B=Atlas 知识 / C=攻略文档）。

    输入：state.user_message / state.trace_id / state.session_id /
          state.extras["session_store"]（可选，用于多轮对话）
    输出：state.classified_pipeline / state.classifier_confidence / state.classifier_model /
          state.turn_type / state.trace_total_tokens

    多轮对话副作用：
    - 加载上一轮 TurnSnapshot 并注入 prev_summary 到分类器 prompt
    - 把 prev_turn 写到 state.extras["prev_turn"]，供下游 merge_filters 节点使用
    - turn_type == MAJOR 时调用 SessionStore.clear_session() 清空遗留状态
    """
    # ── SSE：入口 thinking 事件（流式模式才注入）──
    if state.extras.get("streaming"):
        state.pending_events.append(
            {"type": "thinking", "data": {"phase": "routing", "message": "正在分析问题类型..."}}
        )

    # ── 多轮：加载上一轮快照 ──
    session_store: SessionStore | None = state.extras.get("session_store")
    prev_summary: str | None = None
    if session_store is not None and state.session_id:
        prev_turn = session_store.load_prev_turn(state.session_id)
        if prev_turn is not None:
            state.extras["prev_turn"] = prev_turn
            prev_summary = prev_turn.truncated_summary()

    classifier_prompt = build_classifier_prompt(prev_summary=prev_summary)
    classifier_result = None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            classifier_result = await chat_completion(
                system_prompt=classifier_prompt,
                user_message=state.user_message,
                temperature=0.0,
                json_mode=True,
                response_schema=classifier_response_json_schema,
                response_validator=parse_classifier_response,
            )
            break
        except Exception as cls_err:  # noqa: BLE001
            last_error = cls_err
            if attempt == 0:
                print(f"⚠️ [{state.trace_id}] Stage 0 分类第 1 次尝试失败，重试中: {cls_err}")

    # Stage 0 失败 → 降级走 A 全量路由（兼容旧逻辑：高置信度 1.0 让后续 route_node 处理）
    if classifier_result is None:
        print(f"⚠️ [{state.trace_id}] Stage 0 分类 2 次均失败，降级全量路由: {last_error}")
        state.classified_pipeline = "A"
        state.classifier_confidence = 1.0
        state.classifier_model = "unknown"
        state.turn_type = "MAJOR"
        # 多轮防御：分类失败时也按 MAJOR 处理，主动清状态避免污染
        if session_store is not None and state.session_id:
            session_store.clear_session(state.session_id)
        # BI 维度回填（即使降级也写）
        state.metric_labels.update(
            {
                "pipeline": "A",
                "turn_type": "MAJOR",
                "has_prev_turn": 0,
            }
        )
        return state

    classifier_model = classifier_result.pop("_model", "unknown")
    classifier_result.pop("_response_format", None)
    classifier_result.pop("_provider", None)
    classifier_result.pop("_attempts", None)
    classifier_usage = classifier_result.pop("_usage", {})
    state.trace_total_tokens += classifier_usage.get("total_tokens", 0)
    state.classified_pipeline = classifier_result.get("pipeline", "A")
    state.classifier_confidence = classifier_result.get("confidence", 0.0)
    state.classifier_model = classifier_model

    # ── FALLBACK 后置校验（防误判）──
    # 仅当 LLM 输出 FALLBACK 时执行：检查用户消息是否含 FGO 查询信号，含则强制改回 A
    fallback_code = classifier_result.get("fallback_code")
    if state.classified_pipeline == "FALLBACK":
        if _has_fgo_query_signal(state.user_message or ""):
            print(
                f"⚠️ [{state.trace_id}] classify 后置兜底：LLM 判 FALLBACK 但含查询信号，"
                f"强制改 A (msg={state.user_message!r})"
            )
            state.classified_pipeline = "A"
            fallback_code = None
        elif fallback_code not in ("greeting", "out_of_scope"):
            # FALLBACK 必须带合法 code，缺失则按 greeting 兜底
            fallback_code = "greeting"
    else:
        # 非 FALLBACK 路径必须清空 fallback_code，避免污染
        fallback_code = None
    if fallback_code:
        state.extras["fallback_code"] = fallback_code

    # turn_type 解析（schema 默认 MAJOR；无 prev_turn 强制改回 MAJOR 防御 LLM 误判）
    turn_type = classifier_result.get("turn_type", "MAJOR") or "MAJOR"
    if turn_type not in ("MAJOR", "MINOR", "CORRECTION"):
        turn_type = "MAJOR"
    if "prev_turn" not in state.extras and turn_type != "MAJOR":
        # 没有上一轮上下文却判 MINOR/CORRECTION 是 LLM 漂移，强制纠正为 MAJOR
        turn_type = "MAJOR"

    # 后置规则兜底：MINOR 必须含显式承接/指代词，否则强制改回 MAJOR
    # 防御 LLM 把完整独立查询（如“弓阶的 5 星从者”）误判为追问
    if turn_type == "MINOR":
        msg = (state.user_message or "").strip()
        # 承接/指代词白名单（出现任一即视为有效追问信号）
        # 覆盖三类场景：
        # 1. 显式轮次指代：其中/那些/上面/刚才/前面/上一/刚提
        # 2. 后续动作词：详细/展开/对比/具体/介绍/讲讲/说说/再筛/再看/再帮/再问
        # 3. 代词指称：那他/那她/那它/那个/这个 + 他的/她的/它的
        # 4. 序列词：第一/第二/...
        anchor_keywords = (
            # 轮次指代
            "其中",
            "那些",
            "上面",
            "刚才",
            "前面",
            "上一",
            "刚提",
            "上面那",
            "刚才那",
            # 后续动作词
            "详细",
            "展开",
            "对比",
            "具体",
            "介绍",
            "讲讲",
            "说说",
            "详详细细",
            "再筛",
            "再看",
            "再帮",
            "再问",
            # 代词 / 指示词
            "那他",
            "那她",
            "那它",
            "那个",
            "这个",
            "他的",
            "她的",
            "它的",
            "他们",
            "她们",
            "它们",
            # 序列词
            "第一",
            "第二",
            "第三",
            "第四",
            "第五",
        )
        if not any(kw in msg for kw in anchor_keywords):
            print(f"⚠️ [{state.trace_id}] classify 后置兜底：LLM 判 MINOR 但无承接词，强制改 MAJOR (msg={msg!r})")
            turn_type = "MAJOR"

    state.turn_type = turn_type

    # ── 多轮副作用：MAJOR 时清空 session ──
    if turn_type == "MAJOR" and session_store is not None and state.session_id:
        session_store.clear_session(state.session_id)
        # 已清的 prev_turn 也从 extras 中移除，下游不应再使用
        state.extras.pop("prev_turn", None)

    # BI 维度回填：分类成功路径
    state.metric_labels.update(
        {
            "pipeline": state.classified_pipeline,
            "turn_type": state.turn_type,
            "has_prev_turn": 1 if "prev_turn" in state.extras else 0,
        }
    )

    await log_trace_event(
        state.trace_id,
        "classifier_output",
        {
            "pipeline": state.classified_pipeline,
            "confidence": state.classifier_confidence,
            "turn_type": state.turn_type,
            "has_prev_turn": "prev_turn" in state.extras,
            "model": classifier_model,
            "fallback_code": state.extras.get("fallback_code"),
            "usage": classifier_usage,
        },
    )

    return state
