# Changelog — 项目里程碑

> 记录项目关键节点和版本演进历史。按时间倒序排列。

| 日期 | 事件 | 备注 |
| :--- | :--- | :--- |
| 2026-06-12 | v0.4.3 从者数据库深度升级 | 多宝具从者拆分识别（卫宫/BB迪拜/太空伊什塔尔/托勒密变身）；条件触发效果模型（回合结束/攻击后/延迟触发/中弹反击 等 17 种）；A/B/C/D 类乘区标签烘焙到效果条目；特攻目标特性反查索引（antiTraitIndex）；宝具数值双维度（npValues NP1-5 + ocValues OC1-5）完整保留，D 类宝具特攻按 OC 维度烘焙；玩家视角 function 过滤（剔除仅敌方生效条目）；条件触发常量外置 translations.json；多效果筛选 SSE 描述兼容 effects 数组；修复 obao 推理泄漏 / Agent XML 标签泄漏 / A 类乘区路由误判 / detail_mode 数值不全；新增伤害公式攻略 |
| 2026-06-10 | v0.4.2 全站视觉重构 | 主页/历史日志/管理后台/更新日志四个前端页面统一改造为 FGO 档案室风格（纸白+海军蓝+古金、直角硬阴影、Cinzel+思源宋体）；用户头像替换为芙芙形象；修复 PC 流式期间卡片单列跳变（assistant message-content flex:1 1 auto）+ 移动端长名卡片溢出（grid 子项 min-width:0）+ 响应式降级策略（minmax(280px,1fr) auto-fit，不够自动单列保证信息完整） |
| 2026-06-09 | v0.4.1 戴冠战攻略增强与监控告警 | 戴冠战攻略拆分为通用+职阶专项文档，检索增加职阶 tag 过滤避免跨职阶污染；监控告警 Bark 推送+分层告警+告警历史；技能 CD 字段修复 |
| 2026-06-04 | v0.4.0 技能 CD 查询与数据更新 | 新增技能冷却时间（CD）筛选维度，整合到统一效果搜索中支持 maxCd 参数；同步最新从者映射数据（svt_names 553→555，新增乌尔德等从者） |
| 2026-06-03 | v0.3.12 整合前端快捷入口 | 欢迎消息示例按钮替换为 4 个差异化案例（五星自充Caster/50%初始NP礼装/秩序女性从者/剑阶戴冠战攻略）；删除底部 preset bar 及相关 JS/CSS 逻辑，精简前端代码 ~350 行 |
| 2026-06-01 | v0.3.11 Mooncell 昵称自动同步 | 从 Mooncell Wiki 批量抓取 623 条从者社区昵称，昵称库从 56 条扩充至 600+；Overlay 合并机制（底层自动生成+手工覆盖）；冲突昵称保留多候选配合确认机制 |
| 2026-05-31 | v0.3.10 打赏支持与输入优化 | 新增爱发电/微信/支付宝打赏入口；修复全角字符搜索从者名称无法匹配；修复中文输入法回车误发送 |
| 2026-05-31 | v0.3.9 两阶段路由与稳定性提升 | 两阶段路由架构（ADR-024）：Stage 0 分类器 + Stage 1 参数提取，解决 Prompt 膨胀和误路由；队友效果筛选（ally targetType）；LLM 错误分类与差异化重试；路由重试与调试透明度；前端稳定性修复 |
| 2026-05-27 | v0.3.8 用户确认机制（Clarification） | 执行层反馈式 Clarification — 多候选/空结果/筛选空结果三场景触发选项确认；confirmation_id 精确直达避免死循环；前端 clarification 组件视觉重设计 |
| 2026-05-25 | v0.3.7 知识问答链路与查询精度提升 | A→B→C 三级知识问答链路（攻略文档 BM25 检索）；自充精确匹配/计算修正、技能效果完整展示、职阶别名识别、从者昵称扩展、日志页面样式修复 |
| 2026-05-13 | LLM 昵称识别 + 路由层修复 | 新增 `resolve_nickname` Skill（异步 LLM 识别 + LRU 缓存 + DB 校验），修复路由 Prompt 对疑似从者名称的处理（规则 16），扩充高频昵称映射（红A/红弓/弓凛/枪凛/狮子王等），完整异步 fallback 链集成 |
| 2026-05-12 | 职阶克制查询 + 空结果明示 | 新增 `search_by_class_advantage` Skill（基于 Atlas API 克制关系数据）；OneShot 结果为 0 时传递上下文给 Agent fallback，明确告知空结果+条件列表+放宽建议（Agent tokens 降低 85%） |
| 2026-05-11 | OneShot优先+Agent兜底混合路由 | 回滚 OneShot 路由为主路径，3 个 fallback 点位最小侵入接入 Agent 兜底；llm_client.py 双 SDK 统一封装（dashscope SDK + openai SDK），移除 httpx 手动调用 |
| 2026-05-09 | 使用说明弹窗 | 新增面向普通玩家的使用说明弹窗，首次访问自动弹出，支持可点击示例查询 |
| 2026-05-09 | 虚拟复合效果 | 新增 `damageBoost`（泛用增伤，442从者）和 `damageShield`（挡伤害，320从者）虚拟复合效果 + 路由规则 9 |
| 2026-05-09 | 统一效果搜索 Skill | 新增 `search_by_effect`，默认同时搜技能+宝具效果，修复天草四郎等宝具效果遗漏问题 |
| 2026-05-06 | Phase 5 完成 | P1（Filter Registry、知识配置分离、Chaldea 依赖边界、配置热更新、Trace Debug）+ P2（LLM Retry、前端 UX、异步日志、工程自动化 ruff + GitHub Actions CI） |
| 2026-05-06 | Thinking Steps SSE | 新增 SSE 流式端点，分阶段展示 AI 思考过程（解析→检索→生成），卡片先行渲染 |
| 2026-05-06 | Phase 5 Batch 2 - P0 | 完成数据入口单一化：extractor/np_charge_filter.py 从 191 行降至 52 行，复用 data_loader.py |
| 2026-05-06 | LLM API 迁移 | 从 Chat Completions API 迁移至 OpenAI Responses API（2025 推荐） |
| 2026-05-05 | Phase 5 Batch 1 | 完成 LLM Contract、Query Executor 回归测试、Schema Mirror 回归测试与真实 LLM JSON Schema smoke test |
| 2026-05-05 | Phase 5 启动 | 实现了全链路日志追踪（Logging）与数据预消化（Pre-digestion），补齐了宝具特效解析 |
| 2026-05-05 | Phase 4 完成 | 实现了 Two-Step RAG 架构（生成式 UI），分离了 LLM 总结文案与 UI 数据流 |
| 2026-05-05 | Phase 3 完成 | 实现了多语言映射、特性（Trait）匹配算法、宝具与配卡等从者深层属性过滤 |
| 2026-05-05 | Phase 2 完成 | 实现了 sync_chaldea.py 提取 5 个 Dart 文件的效果知识并与 LLM 集成 |
| 2026-05-05 | 架构升级 | 确立 Schema Mirror 策略，目标对标 Chaldea 全数据查询 |
| 2026-05-05 | AI Native v1 | 对话式查询上线（FastAPI + LLM 意图解析 + 从者卡片） |
| 2026-05-05 | Demo v1 完成 | 30% NP 自充筛选器 (Python + Web) |
| 2026-05-05 | 项目初始化 | 创建 OpenClaw 风格的项目骨架 |
