# ADR-021: Agentic Tool Use 路由模式

## 状态
已实现 (2026-05-11)，待验证合并（分支 `feat/agentic-tool-use-routing`）

## 背景
当前 One-Shot Prompt 路由模式（300 行 Prompt + 13 条规则）存在以下问题：
- 每新增一个业务场景都需要手动补规则，维护成本线性增长
- LLM 一步到位解析意图，无法自我纠正（效果名拼错则直接返回空结果）
- 无法按需反查 Atlas API 获取 MV 中不存在的低频数据（如 Lv6 技能数值）

## 决策
新增 Agentic Tool Use 路由模式，与现有 One-Shot 模式通过环境变量 `ROUTING_MODE=agent|oneshot` 并行共存。

### 核心设计
- **协议层**：使用 Chat Completions API（`/chat/completions`），而非 Responses API。dashscope 的 Responses API 兼容层不支持多轮 `function_call_output` 协议。
- **Agent Loop**：`server/agent/agent_loop.py` 负责多轮 tool 调用编排，messages 累积式构建，max_rounds=5 保护。
- **Tool 定义**：`server/agent/tool_defs.py` 生成 Chat Completions 格式的 7 个 tools（search_servants、lookup_servant、compare_servants、list_effects、list_traits、list_classes、lookup_skill_detail）。
- **Tool Handlers**：`server/agent/tool_handlers.py` 桥接 Agent Loop 和现有 SkillExecutor，复用所有 Skill 模块。
- **System Prompt**：~30 行极简指令（`server/agent/agent_prompt.py`），替代 300 行路由 Prompt。效果映射知识由 tool description 承载。

### 关键优势
1. **自我纠正**：LLM 不确定效果名时可先调 `list_effects` 查表，再用正确的 key 搜索
2. **按需反查**：`lookup_skill_detail` 可 runtime 调用 Atlas API 获取任意等级技能数值
3. **零规则维护**：新增 Skill 只需在 tool_defs 和 tool_handlers 中追加定义，无需修改 Prompt 规则

## 影响
- 新增 `server/agent/` 包（4 个文件）
- `server/llm_client.py` 新增 `agent_completion()` 和 `_post_agent_chat()`
- `server/main.py` 新增 `_handle_agent_mode()` 分支
- 修复 `search_by_effect.py` 多效果模式下复合效果不展开的 bug
- 27 个新测试（test_agent_loop + test_tool_handlers）

## 权衡
- Agent 模式平均 2-4 轮 LLM 调用（vs oneshot 模式 1 轮路由 + 1 轮生成），Token 消耗更高
- 但省去了独立的生成 LLM 调用（Agent 最后一轮直接生成回复），部分抵消额外成本
- 延迟略增（每轮 ~1s），但用户感知上有"思考过程"展示
