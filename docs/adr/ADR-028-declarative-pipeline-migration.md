# ADR-028: 声明式管线架构迁移 + 多轮对话状态设计

**状态**: 已设计，待实施  
**日期**: 2026-06-12  
**决策者**: 羽殊  
**关联 ADR**: ADR-024（两阶段路由）、ADR-025（多轮状态调研）、ADR-027（LangGraph 评估）

---

## 一、背景与动机

### 1.1 当前架构的核心问题

`server/pipeline.py`（2318 行）是 Laplace 的请求处理核心。当前采用命令式控制流，存在三个结构性问题：

**问题 1：JSON/SSE 双模式代码高度重复**

`handle_skill_mode()`（~900 行）和 `stream_event_generator()`（~900 行）实现了几乎相同的业务逻辑，只是输出方式不同（返回 ChatResponse vs yield SSE 事件）。每次修改业务逻辑都需要同步修改两处，极易遗漏。

**问题 2：降级逻辑分散重复**

Agent 兜底逻辑在以下 4 个位置各出现一次，代码高度相似但不完全相同：
- Stage 0 低置信度降级（`pipeline.py:208-246`）
- Stage 1 路由失败降级（`pipeline.py:272-329`）
- 技能执行空结果降级（`pipeline.py:625-685`）
- SSE 模式对应的 3 个位置（`pipeline.py:1157-1782`）

每处都带着重复的 `log_trace_event()`、`classify_agent_reply()`、`ChatResponse` 构建代码。

**问题 3：无法支持多轮对话**

后端完全无状态，每次请求独立处理。`ChatRequest` 没有 `session_id` 或历史消息字段。用户说"其中有哪些是弓阶的？"时系统不知道"其中"指什么。唯一的状态延续是 `confirmation_context`（澄清选择后直通查库），这是一个硬编码的特殊分支（`pipeline.py:806-991`）。

### 1.2 参考来源

参考 AI-DATA（NL2Data）平台架构文档中的设计模式（完整文档见钉钉：`https://alidocs.dingtalk.com/i/nodes/a9E05BDRVQRkezKGCyr5wlPGJ63zgkYA`）：

- **有向状态图**：用工程规则控制节点流转，支持条件边和回退。LLM 只负责语义理解，路由和关键决策由工程规则完成
- **Harness Agent 模式**：LLM 是能力引擎，确定性服务是缰绳。与 Laplace 的设计哲学一致
- **MINOR/MAJOR 意图分类**：MINOR（追问）复用上轮状态，MAJOR（新问题）走完整链路
- **踩坑经验**：标记位在多分支场景下容易残留，所有触发新问题的路径必须显式清除标记

### 1.3 LangGraph 评估结论（ADR-027）

评估了引入 LangGraph 框架的方案，结论是**不采用 LangGraph**，理由：

1. **依赖风险**：引入 80+ 传递依赖，LangChain 生态 breaking changes 频繁
2. **LLM 容灾层不兼容**：当前自研的两层降级（同供应商多模型 → 跨供应商）比 LangChain 的 `with_fallbacks()` 更精细，迁移需要放弃或重新适配
3. **技能系统正交**：LangGraph 不提供域分组、AND 合并过滤、4 级降级等能力，这些代码无论如何必须保留
4. **图复杂度不高**：Laplace 全展开约 10-12 个节点，不需要通用框架

**决策：自研轻量声明式图引擎**（~400 行 Python），借鉴 LangGraph 的核心抽象（StateGraph / Node / ConditionalEdge / Checkpointer），但零外部依赖。

---

## 二、目标架构设计

### 2.1 核心抽象（4 个概念）

#### 概念 1：类型化状态（PipelineState）

```python
@dataclass
class PipelineState:
    """流经整个图的状态对象。每个节点读取和更新它。"""
    # ── 输入 ──
    message: str                       # 用户原始输入
    trace_id: str                      # 请求追踪 ID
    client_ip: str = "unknown"
    
    # ── 多轮上下文（Phase 5 加入）──
    session_id: str | None = None      # 会话 ID
    turn_type: str = "MAJOR"           # MAJOR/MINOR/CORRECTION
    prev_turn: TurnSnapshot | None = None  # 上一轮快照
    
    # ── Stage 0 输出 ──
    pipeline: str = ""                 # A/B/C
    confidence: float = 0.0
    guide_tags: list[str] = field(default_factory=list)
    
    # ── Stage 1 输出 ──
    skill_calls: list[dict] = field(default_factory=list)
    response_skill_name: str = "respond_servant_list"
    routing_fallback: dict | None = None
    routing_clarification: dict | None = None
    
    # ── 执行输出 ──
    execution_result: Any = None       # ExecutionResult
    
    # ── 生成输出 ──
    reply: str = ""
    servants: list[dict] = field(default_factory=list)
    count: int = 0
    
    # ── 元数据 ──
    total_tokens: int = 0
    model_used: str = ""
    error: str | None = None
    mode: str = "oneshot"              # oneshot/agent_fallback/...
    
    # ── SSE 事件队列（流式模式用）──
    events: list = field(default_factory=list)
```

与命令式架构中散落在函数体各处的局部变量不同，`PipelineState` 将所有状态集中到一个类型化对象中。每个节点明确声明它读取和写入的字段。

#### 概念 2：节点 = 异步纯函数（State → State）

```python
# 每个节点只做一件事，输入 state，返回更新后的 state
async def classify_node(state: PipelineState) -> PipelineState:
    """Stage 0: 三路分类。"""
    result = await chat_completion(
        system_prompt=build_classifier_prompt(prev_summary=state.prev_turn),
        user_message=state.message,
        temperature=0.0,
        json_mode=True,
        response_schema=classifier_response_json_schema,
        response_validator=parse_classifier_response,
    )
    state.pipeline = result.get("pipeline", "A")
    state.confidence = result.get("confidence", 0.0)
    state.guide_tags = result.get("tags", [])
    state.total_tokens += result.get("_usage", {}).get("total_tokens", 0)
    return state
```

**流式节点**用 async generator，同时产出 SSE 事件和状态更新：

```python
async def generate_stream_node(state: PipelineState):
    """RAG 生成（流式版）。"""
    yield SSEEvent("thinking", {"step": "正在生成回复..."})
    
    prompt = build_generation_prompt(state)
    async for chunk in chat_completion_stream(
        system_prompt=prompt,
        user_message=state.message,
        temperature=0.1,
    ):
        yield SSEEvent("delta", {"content": chunk})
    
    yield SSEEvent("done", {})
    yield state  # 最后一个 yield 是更新后的 state
```

#### 概念 3：条件边 = 纯函数（State → 节点名）

```python
def after_classify(state: PipelineState) -> str:
    """Stage 0 之后的分发逻辑。"""
    if state.error:                    return "agent_fallback"
    if state.pipeline == "B":          return "atlas"
    if state.pipeline == "C":          return "guide"
    if state.confidence < 0.6:         return "agent_fallback"
    if state.turn_type == "MINOR":     return "merge_filters"  # 多轮追问
    return "route"

def after_route(state: PipelineState) -> str:
    """Stage 1 之后的分发逻辑。"""
    if state.error:                    return "agent_fallback"
    if state.routing_clarification:    return "clarify"
    fb = state.routing_fallback
    if fb and fb.get("code") in ("greeting", "out_of_scope"):
        return "template_reply"
    if fb:                             return "agent_fallback"
    if not state.skill_calls:          return "agent_fallback"
    return "execute"

def after_execute(state: PipelineState) -> str:
    """技能执行之后的分发逻辑（降级链入口）。"""
    r = state.execution_result
    if r and r.total_found > 0:        return "generate"
    if r and r.clarification:          return "resolve_nick"
    if r and r.is_fallback:            return "resolve_nick"
    return "generate"

def after_resolve_nick(state: PipelineState) -> str:
    """昵称解析之后。"""
    r = state.execution_result
    if r and r.total_found > 0:        return "generate"
    if r and r.clarification:          return "guess_candidate"
    return "guess_candidate"

def after_guess(state: PipelineState) -> str:
    """LLM 候选猜测之后。"""
    r = state.execution_result
    if r and r.total_found > 0:        return "generate"
    if r and r.clarification:          return "clarify"
    return "agent_fallback"

def after_agent(state: PipelineState) -> str:
    """Agent 兜底之后。"""
    if state.reply:                    return END
    return "static_fallback"
```

所有分发规则集中在一个文件中（`edges.py`），一眼可见完整的路由拓扑。

#### 概念 4：图定义 = 节点 + 边的声明

```python
def build_pipeline_graph() -> StateGraph:
    """组装完整的管线图。"""
    graph = StateGraph()
    
    # ── 节点注册 ──
    graph.add_node("classify",        with_retry(2)(classify_node))
    graph.add_node("route",           with_retry(2)(route_node))
    graph.add_node("execute",         execute_node)
    graph.add_node("resolve_nick",    resolve_nick_node)
    graph.add_node("guess_candidate", guess_candidate_node)
    graph.add_node("generate",        generate_node)
    graph.add_node("agent_fallback",  agent_fallback_node)   # 只写一次
    graph.add_node("static_fallback", static_fallback_node)
    graph.add_node("clarify",         clarify_node)
    graph.add_node("template_reply",  template_reply_node)
    graph.add_node("atlas",           atlas_node)
    graph.add_node("guide",           guide_node)
    graph.add_node("merge_filters",   merge_filters_node)   # 多轮追问专用
    graph.add_node("confirmation",    confirmation_node)     # 澄清确认直通
    
    # ── 条件边 ──
    graph.set_entry("classify")
    graph.add_edges("classify",        after_classify)
    graph.add_edges("route",           after_route)
    graph.add_edges("execute",         after_execute)
    graph.add_edges("resolve_nick",    after_resolve_nick)
    graph.add_edges("guess_candidate", after_guess)
    graph.add_edges("agent_fallback",  after_agent)
    
    # ── 直达边（无条件）──
    graph.add_edge("merge_filters",    "execute")
    graph.add_edge("template_reply",   END)
    graph.add_edge("static_fallback",  END)
    graph.add_edge("clarify",          END)
    graph.add_edge("generate",         END)
    graph.add_edge("confirmation",     "generate")
    graph.add_edge("atlas",            END)
    graph.add_edge("guide",            END)
    
    return graph
```

### 2.2 图引擎实现

```python
END = "__end__"

class StateGraph:
    """轻量声明式图引擎。"""
    
    def __init__(self):
        self._nodes: dict[str, Callable] = {}
        self._conditional_edges: dict[str, Callable] = {}
        self._direct_edges: dict[str, str] = {}
        self._entry: str = ""
        self._checkpointer: Checkpointer | None = None
    
    def add_node(self, name: str, fn: Callable) -> None:
        self._nodes[name] = fn
    
    def add_edges(self, source: str, dispatch_fn: Callable) -> None:
        self._conditional_edges[source] = dispatch_fn
    
    def add_edge(self, source: str, target: str) -> None:
        self._direct_edges[source] = target
    
    def set_entry(self, name: str) -> None:
        self._entry = name
    
    def set_checkpointer(self, cp: Checkpointer) -> None:
        self._checkpointer = cp
    
    def _next_node(self, current: str, state: PipelineState) -> str | None:
        """根据条件边或直达边确定下一节点。"""
        if current in self._conditional_edges:
            target = self._conditional_edges[current](state)
            return None if target == END else target
        if current in self._direct_edges:
            target = self._direct_edges[current]
            return None if target == END else target
        return None
    
    async def run(self, state: PipelineState) -> PipelineState:
        """非流式执行：返回最终状态。"""
        current = self._entry
        while current:
            node_fn = self._nodes[current]
            state = await node_fn(state)
            # 检查点保存
            if self._checkpointer:
                await self._checkpointer.save(state, current)
            current = self._next_node(current, state)
        return state
    
    async def run_stream(self, state: PipelineState) -> AsyncIterator:
        """流式执行：yield SSE 事件，最终返回状态。"""
        current = self._entry
        while current:
            node_fn = self._nodes[current]
            if inspect.isasyncgenfunction(node_fn):
                # 流式节点：yield 事件，最后一个非事件 yield 是 state
                async for item in node_fn(state):
                    if isinstance(item, SSEEvent):
                        yield item
                    elif isinstance(item, PipelineState):
                        state = item
            else:
                state = await node_fn(state)
            if self._checkpointer:
                await self._checkpointer.save(state, current)
            current = self._next_node(current, state)
    
    async def resume(self, state: PipelineState, from_node: str) -> PipelineState:
        """从指定节点恢复执行（多轮对话 MINOR 回退用）。"""
        current = from_node
        while current:
            node_fn = self._nodes[current]
            state = await node_fn(state)
            if self._checkpointer:
                await self._checkpointer.save(state, current)
            current = self._next_node(current, state)
        return state
```

### 2.3 节点装饰器（横切关注点）

**重试装饰器**（替代当前分散的 `for range(2)` 循环）：

```python
def with_retry(max_attempts: int = 2):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(state: PipelineState) -> PipelineState:
            last_error = None
            for attempt in range(max_attempts):
                try:
                    return await fn(state)
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts - 1:
                        print(f"⚠️ [{state.trace_id}] {fn.__name__} 第 {attempt+1} 次失败，重试: {e}")
            state.error = str(last_error)
            return state
        return wrapper
    return decorator
```

**日志装饰器**（替代当前内联的 `log_trace_event` 调用）：

```python
def with_trace(phase: str):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(state: PipelineState) -> PipelineState:
            start = time.monotonic()
            result = await fn(state)
            elapsed = (time.monotonic() - start) * 1000
            await log_trace_event(state.trace_id, phase, {
                "elapsed_ms": elapsed,
                "node": fn.__name__,
                "total_tokens": state.total_tokens,
            })
            return result
        return wrapper
    return decorator
```

---

## 三、多轮对话状态设计

### 3.1 MINOR/MAJOR 意图分类

在 `classify_node` 中扩展分类器输出，增加 `turn_type` 字段：

```python
# 分类器 prompt 扩展（注入上轮摘要）
def build_classifier_prompt(prev_summary: str | None = None) -> str:
    base_prompt = ...  # 现有分类器 prompt
    if prev_summary:
        base_prompt += f"""
## 多轮对话判断

上一轮对话摘要：{prev_summary}

请额外判断用户的新消息属于以下哪种类型：
- MAJOR：新问题，与上轮无关或切换了查询主题
- MINOR：追问/补充/细化，在上轮结果基础上调整条件
- CORRECTION：纠正上轮的错误理解（如名字歧义）

在输出 JSON 中增加 "turn_type" 字段。
"""
    return base_prompt

# 分类器输出扩展
{
    "pipeline": "A",
    "confidence": 0.85,
    "turn_type": "MINOR",    // 新增
    "tags": [...]
}
```

### 3.2 状态快照与会话存储

```python
@dataclass
class TurnSnapshot:
    """单轮处理的关键状态快照。"""
    pipeline: str
    skill_calls: list[dict]
    response_skill: str
    execution_result_summary: dict   # 精简版（不含完整数据库记录）
    query_summary: str               # 人类可读摘要（注入下轮分类器）
    servants_returned: list[int]     # collectionNo 列表
    timestamp: float

class SessionStore:
    """内存会话存储，带 TTL 自动清理。"""
    
    def __init__(self, ttl_seconds: int = 1800, max_sessions: int = 1000):
        self._sessions: dict[str, list[TurnSnapshot]] = {}
        self._timestamps: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._max = max_sessions
    
    def save_turn(self, session_id: str, snapshot: TurnSnapshot) -> None:
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        self._sessions[session_id].append(snapshot)
        # 只保留最近 5 轮
        self._sessions[session_id] = self._sessions[session_id][-5:]
        self._timestamps[session_id] = time.time()
        self._cleanup()
    
    def get_prev_turn(self, session_id: str) -> TurnSnapshot | None:
        turns = self._sessions.get(session_id, [])
        return turns[-1] if turns else None
    
    def clear_session(self, session_id: str) -> None:
        """MAJOR 新问题时显式清除上轮状态。"""
        self._sessions.pop(session_id, None)
        self._timestamps.pop(session_id, None)
    
    def _cleanup(self) -> None:
        now = time.time()
        expired = [k for k, t in self._timestamps.items() if now - t > self._ttl]
        for k in expired:
            self._sessions.pop(k, None)
            self._timestamps.pop(k, None)
        # 超过上限时淘汰最旧的
        while len(self._sessions) > self._max:
            oldest = min(self._timestamps, key=self._timestamps.get)
            self._sessions.pop(oldest, None)
            self._timestamps.pop(oldest, None)
```

### 3.3 MINOR 追问的图路径

MINOR 追问时，从 `classify` 节点通过条件边跳到 `merge_filters` 节点，跳过 `route`（Stage 1），直接复用上轮的 `skill_calls` 并追加新的过滤条件：

```python
async def merge_filters_node(state: PipelineState) -> PipelineState:
    """MINOR 追问：复用上轮 skill_calls，追加新条件。"""
    prev = state.prev_turn
    if not prev:
        # 无上轮状态，降级为 MAJOR
        state.turn_type = "MAJOR"
        return state
    
    # 复用上轮的技能调用
    state.skill_calls = prev.skill_calls.copy()
    state.response_skill_name = prev.response_skill
    
    # 用 LLM 从用户新消息中提取追加/修改的参数
    delta = await extract_minor_delta(state.message, prev.query_summary)
    
    if delta.get("add_filters"):
        # 追加过滤条件（如 "其中弓阶的" → 追加 search_by_class）
        state.skill_calls.extend(delta["add_filters"])
    
    if delta.get("modify_params"):
        # 修改现有参数（如 "我说的是Alter版" → 修改 name 参数）
        for mod in delta["modify_params"]:
            for call in state.skill_calls:
                if call["skill"] == mod["skill"]:
                    call["params"].update(mod["params"])
    
    if delta.get("change_response"):
        # 切换回复技能（如 "详细说说" → respond_servant_detail）
        state.response_skill_name = delta["change_response"]
    
    return state

# 条件边中的分发
def after_classify(state: PipelineState) -> str:
    # ...
    if state.turn_type == "MINOR" and state.prev_turn:
        return "merge_filters"    # 跳过 route，直接复用
    if state.turn_type == "CORRECTION" and state.prev_turn:
        return "route"            # 需要重新路由，但带上纠正上下文
    return "route"                # MAJOR 走完整链路
```

### 3.4 状态一致性保障

借鉴 AI-DATA 的踩坑经验，显式管理状态清除：

```python
# 在 classify_node 中
async def classify_node(state: PipelineState) -> PipelineState:
    # ... 分类逻辑 ...
    
    # 关键：MAJOR 新问题时显式清除上轮状态
    if state.turn_type == "MAJOR":
        state.prev_turn = None
        state.skill_calls = []
        state.execution_result = None
        if state.session_id:
            session_store.clear_session(state.session_id)
    
    return state
```

### 3.5 完整的多轮流程图

```
用户输入 + session_id
    │
    ▼
┌───────────┐
│ classify   │ ← 注入 prev_turn.query_summary 到分类器 prompt
└─────┬─────┘   输出: pipeline + turn_type
      │
      ├── MAJOR ──────────────→ route → execute → ... (完整链路)
      │
      ├── MINOR ──────────────→ merge_filters → execute → resolve → ... → generate
      │                          (跳过 route，复用上轮 skill_calls)
      │
      ├── CORRECTION ─────────→ route → execute → ... (重新路由，带纠正上下文)
      │
      ├── Pipeline B ─────────→ atlas (注入上轮上下文到 Agent prompt)
      │
      └── Pipeline C ─────────→ guide (注入上轮上下文到检索 query)
```

---

## 四、ChatRequest 接口变更

```python
class ChatRequest(BaseModel):
    message: str
    mode: str = "skill"
    preset_name: str | None = None
    params: dict | None = None
    response_skill: str | None = None
    confirmation_context: dict | None = None
    # ── 新增 ──
    session_id: str | None = None      # 会话 ID（前端生成 UUID）
```

前端 `app.js` 变更：
- 每个会话在创建时生成一个 UUID 作为 `session_id`
- 每次请求携带 `session_id`
- 前端不再需要拼装 `confirmation_context` 文本——Checkpointer 会保留完整状态

---

## 五、重构后的文件结构

```
server/
├── graph/
│   ├── __init__.py
│   ├── engine.py              # StateGraph 图引擎（~200 行）
│   ├── state.py               # PipelineState 数据类（~80 行）
│   ├── session.py             # SessionStore + TurnSnapshot（~120 行）
│   └── decorators.py          # with_retry, with_trace 装饰器（~60 行）
├── nodes/
│   ├── __init__.py
│   ├── classify.py            # Stage 0 分类节点（~80 行）
│   ├── route.py               # Stage 1 路由节点（~100 行）
│   ├── execute.py             # 技能执行节点（~80 行）
│   ├── resolve.py             # 昵称解析 + 候选猜测（~100 行）
│   ├── generate.py            # RAG 生成节点（~120 行）
│   ├── agent.py               # Agent 兜底节点（~80 行，只写一次）
│   ├── atlas.py               # Pipeline B 节点（~100 行）
│   ├── guide.py               # Pipeline C 节点（~100 行）
│   ├── merge_filters.py       # MINOR 追问过滤合并（~80 行）
│   ├── clarify.py             # 澄清/确认节点（~60 行）
│   └── fallback.py            # 模板回复 + 静态兜底（~40 行）
├── edges.py                   # 所有条件边定义（~120 行）
├── pipeline.py                # 图组装 + 入口函数（~150 行，原 2318 行）
├── skills/                    # 完全不变（25 个技能 + executor + presets）
├── llm/                       # 完全不变（provider + 3 个 adapter）
├── prompts.py                 # 微调：分类器 prompt 增加 turn_type 支持
├── context_builder.py         # 不变
├── query_executor.py          # 不变
├── translation.py             # 不变
└── ...
```

### 保持不变的模块

| 模块 | 行数 | 说明 |
|:---|:---|:---|
| `server/skills/` 全部 | ~3700 行 | 25 个技能 + executor + presets + base |
| `server/llm/` 全部 | ~1400 行 | provider + base + 3 个 adapter |
| `server/prompts.py` | 521 行 | 仅扩展分类器 prompt 的 turn_type 部分 |
| `server/context_builder.py` | 366 行 | 不变 |
| `server/query_executor.py` | 366 行 | 不变 |
| `server/translation.py` | 231 行 | 不变 |
| `server/guide_retriever.py` | 176 行 | 不变 |
| `server/schemas.py` | 151 行 | 扩展 ClassifierResponse 增加 turn_type |
| `server/agent/` 全部 | ~600 行 | 不变 |
| `server/monitor/` 全部 | ~750 行 | 不变 |
| `demo/` 前端 | ~3000 行 | 仅增加 session_id 发送 |

---

## 六、迁移策略：5 个 Phase

### Phase 1：图引擎骨架 + Pipeline B/C 迁移

**目标**：验证图引擎可行性，最小风险

**改动**：
- 新建 `server/graph/engine.py`（StateGraph 核心）
- 新建 `server/graph/state.py`（PipelineState）
- 新建 `server/nodes/atlas.py` 和 `server/nodes/guide.py`
- `pipeline.py` 中增加 feature flag：`USE_GRAPH_ENGINE = os.getenv("USE_GRAPH_ENGINE", "false")`

**验证**：Pipeline B/C 通过图引擎执行，结果与原路径一致

### Phase 2：Pipeline A 主路径迁移

**目标**：将 classify → route → execute → generate 的主路径迁移到图引擎

**改动**：
- 新建 `server/nodes/classify.py`、`route.py`、`execute.py`、`generate.py`
- 新建 `server/edges.py`（条件边定义）
- `pipeline.py` 中的图组装函数 `build_pipeline_graph()`

**验证**：Pipeline A 的正常路径（无降级、无澄清）通过图引擎执行

### Phase 3：降级路径迁移

**目标**：将 4 级降级链统一为节点链

**改动**：
- 新建 `server/nodes/resolve.py`、`agent.py`、`clarify.py`、`fallback.py`
- `edges.py` 中增加降级条件边

**验证**：所有降级场景（名字未匹配、空结果、路由失败）行为一致

### Phase 4：JSON/SSE 双模式统一

**目标**：消除 `handle_skill_mode()` 和 `stream_event_generator()` 的代码重复

**改动**：
- 图引擎增加 `run_stream()` 方法
- 流式节点改为 async generator
- `pipeline.py` 统一入口：`graph.run()` vs `graph.run_stream()`
- 删除旧的 `handle_skill_mode()` 和 `stream_event_generator()`

**验证**：JSON 和 SSE 模式都通过统一的图引擎执行

### Phase 5：多轮对话状态

**目标**：实现 MINOR/MAJOR 分类 + 会话状态管理

**改动**：
- 新建 `server/graph/session.py`（SessionStore + TurnSnapshot）
- 新建 `server/nodes/merge_filters.py`（MINOR 追问节点）
- `prompts.py` 扩展分类器 prompt（注入 prev_summary + turn_type 输出）
- `schemas.py` 扩展 ClassifierResponse（增加 turn_type）
- `main.py` 的 ChatRequest 增加 `session_id`
- `demo/app.js` 发送 `session_id`
- `edges.py` 增加 MINOR/CORRECTION 条件边

**验证**：
- "哪些从者有充能技能？" → "其中弓阶的" → 正确追加过滤
- "哪些从者有充能技能？" → "黑贞的技能是什么" → 识别为新问题走完整链路
- "查一下伊织" → 返回错误从者 → "我说的是FGO的伊织" → 纠正名字

---

## 七、完整图拓扑可视化

```
                        ┌────────────────┐
                        │   classify      │ ← entry point
                        │  (Stage 0)      │
                        └───────┬────────┘
                                │
              ┌─────────────────┼─────────────────┐─────────────┐
              │                 │                  │             │
         pipeline=B        pipeline=C         confidence<0.6   pipeline=A
              │                 │                  │             │
              ▼                 ▼                  │    ┌────────┴────────┐
        ┌──────────┐     ┌──────────┐              │   MINOR          MAJOR/
        │  atlas    │     │  guide   │              │    │           CORRECTION
        │(Pipeline B)│    │(Pipeline C)│            │    ▼              │
        └──────────┘     └──────────┘              │ ┌──────────┐     ▼
                                                   │ │merge_     │ ┌────────┐
                                                   │ │ filters   │ │ route  │
                                                   │ └─────┬─────┘ │(Stage 1)│
                                                   │       │       └───┬────┘
                                                   │       │           │
                                                   │       │     ┌─────┼──────┐
                                                   │       │  fallback  │  clarify
                                                   │       │     │      │     │
                                                   │       │     ▼      │     ▼
                                                   │       │ ┌────────┐ │  ┌──────┐
                                                   │       │ │template│ │  │clarify│
                                                   │       │ │ reply  │ │  └──────┘
                                                   │       │ └────────┘ │
                                                   │       │            │
                                                   │       └──────┬─────┘
                                                   │              │
                                                   │              ▼
                                                   │       ┌──────────┐
                                                   │       │ execute   │ ← 技能执行
                                                   │       └─────┬────┘
                                                   │             │
                                                   │    ┌────────┼────────┐
                                                   │  found>0    │     empty
                                                   │    │        │        │
                                                   │    │        │        ▼
                                                   │    │        │  ┌───────────┐
                                                   │    │        │  │resolve_   │ Level 1
                                                   │    │        │  │  nick     │ 昵称缓存
                                                   │    │        │  └─────┬─────┘
                                                   │    │        │        │
                                                   │    │        │   ┌────┼──────┐
                                                   │    │        │ found>0│    still empty
                                                   │    │        │   │    │       │
                                                   │    │        │   │    │       ▼
                                                   │    │        │   │    │ ┌───────────┐
                                                   │    │        │   │    │ │guess_     │ Level 2
                                                   │    │        │   │    │ │candidate  │ LLM 猜测
                                                   │    │        │   │    │ └─────┬─────┘
                                                   │    │        │   │    │       │
                                                   │    │        │   │    │  ┌────┼──────┐
                                                   │    │        │   │    │found>0│   still empty
                                                   │    │        │   │    │  │    │      │
                                                   ▼    ▼        ▼   ▼    ▼  ▼    │      ▼
                                              ┌──────────────────────────────┐│┌──────────┐
                                              │         generate             │││  agent    │ Level 3
                                              │       (RAG 生成)             │││ fallback  │
                                              └──────────────────────────────┘│└─────┬────┘
                                                                              │      │
                                                                              │ ┌────┼────┐
                                                                              │ ok   │  fail
                                                                              │ │    │    │
                                                                              │ │    │    ▼
                                                                              │ │    │┌────────┐
                                                                              │ │    ││static  │ Level 4
                                                                              │ │    ││fallback│
                                                                              │ │    │└────────┘
                                                                              │ └────┘
                                                                              └──→ clarify
```

---

## 八、验证方案

### 每个 Phase 的验证清单

1. **单元测试**：每个节点函数独立测试（输入 state → 验证输出 state 的字段变更）
2. **图拓扑测试**：验证条件边在各种 state 组合下返回正确的目标节点
3. **集成测试**：端到端请求，对比重构前后的响应一致性
4. **SSE 兼容性测试**：验证 6 种事件类型的顺序和内容不变
5. **降级路径测试**：mock LLM 失败，验证降级行为一致
6. **回归测试**：`pytest tests/ -v` 全量通过
7. **Feature flag 回滚**：`USE_GRAPH_ENGINE=false` 时回退到原路径

### Phase 5 多轮对话专项测试

| 场景 | 输入序列 | 预期行为 |
|:---|:---|:---|
| 追加过滤 | "充能技能" → "其中弓阶" | MINOR，复用 skill_calls + 追加 search_by_class |
| 切换回复 | "查伊织" → "详细说说" | MINOR，复用结果，切换 respond_servant_detail |
| 名字纠正 | "查伊织" → "FGO的伊织" | CORRECTION，重新路由带纠正上下文 |
| 新问题 | "充能技能" → "黑贞的技能" | MAJOR，清除上轮状态走完整链路 |
| 跨管线 | "查剑R" → "剑阶戴冠怎么配" | MAJOR（A→C），走完整链路 |
| 会话超时 | 30 分钟无活动后追问 | session 已清理，按 MAJOR 处理 |
