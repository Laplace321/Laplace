# AI集成架构

<cite>
**本文档引用的文件**
- [llm_client.py](file://server/llm_client.py)
- [prompts.py](file://server/prompts.py)
- [main.py](file://server/main.py)
- [schemas.py](file://server/schemas.py)
- [query_executor.py](file://server/query_executor.py)
- [logger.py](file://server/logger.py)
- [test_llm_client.py](file://tests/test_llm_client.py)
- [test_llm_client_live.py](file://tests/test_llm_client_live.py)
- [effect_schema.json](file://server/knowledge/effect_schema.json)
- [nicknames.json](file://server/knowledge/nicknames.json)
- [individuality.py](file://server/individuality.py)
- [data_loader.py](file://server/data_loader.py)
</cite>

## 目录
1. [简介](#简介)
2. [项目结构](#项目结构)
3. [核心组件](#核心组件)
4. [架构概览](#架构概览)
5. [详细组件分析](#详细组件分析)
6. [依赖关系分析](#依赖关系分析)
7. [性能考量](#性能考量)
8. [故障排除指南](#故障排除指南)
9. [结论](#结论)

## 简介

Laplace项目采用双阶段AI处理架构，专为FGO（命运冠位指定）从者数据查询而设计。该架构通过LLM客户端实现多模型支持，结合严格的提示词工程和结构化输出验证，确保从自然语言到精确查询的可靠转换。

系统采用两阶段处理流程：第一阶段进行意图解析和结构化查询生成，第二阶段进行自然语言生成和RAG（检索增强生成）。这种设计既保证了查询的准确性，又提供了流畅的用户体验。

## 项目结构

```mermaid
graph TB
subgraph "服务器层"
A[main.py<br/>FastAPI应用]
B[llm_client.py<br/>LLM客户端]
C[prompts.py<br/>提示词管理]
D[schemas.py<br/>数据模式]
E[query_executor.py<br/>查询执行器]
F[logger.py<br/>日志记录]
end
subgraph "知识库"
G[effect_schema.json<br/>效果分类]
H[nicknames.json<br/>昵称映射]
I[individuality.py<br/>特性检查]
end
subgraph "数据层"
J[data_loader.py<br/>数据加载器]
K[servants_db.json<br/>从者数据库]
end
A --> B
A --> C
A --> E
A --> F
B --> D
C --> G
E --> H
E --> I
J --> K
K --> E
```

**图表来源**
- [main.py:1-365](file://server/main.py#L1-L365)
- [llm_client.py:1-254](file://server/llm_client.py#L1-L254)
- [prompts.py:1-219](file://server/prompts.py#L1-L219)

**章节来源**
- [main.py:114-148](file://server/main.py#L114-L148)
- [llm_client.py:24-34](file://server/llm_client.py#L24-L34)

## 核心组件

### LLM客户端架构

LLM客户端采用响应式设计，支持多模型回退和结构化输出验证：

```mermaid
classDiagram
class LLMClient {
+str BASE_URL
+str API_KEY
+str PRIMARY_MODEL
+list FALLBACK_MODELS
+chat_completion(system_prompt, user_message, model, max_tokens, temperature, json_mode) dict
+parse_intent_response(content) dict
+extract_json_object(content) str
}
class IntentResponse {
+Literal intent
+QueryConditions conditions
+str responseTemplate
+model_validate(data) IntentResponse
}
class QueryConditions {
+NumericCondition npCharge
+NumericCondition rarity
+str className
+str name
+str[] names
+str skillEffect
+str[] skillEffects
+Literal skillEffectsOp
+Literal targetType
+int[] traits
+int[] excludeTraits
+Literal gender
+Literal attribute
+dict cards
+Literal npCard
+Literal npTarget
}
LLMClient --> IntentResponse : "验证"
IntentResponse --> QueryConditions : "包含"
```

**图表来源**
- [llm_client.py:41-132](file://server/llm_client.py#L41-L132)
- [schemas.py:79-92](file://server/schemas.py#L79-L92)

### 提示词工程体系

系统采用分层提示词设计，包含系统提示词和生成提示词：

```mermaid
flowchart TD
A[用户输入] --> B[系统提示词构建]
B --> C[效果分类注入]
C --> D[查询规则定义]
D --> E[输出格式约束]
E --> F[意图解析阶段]
G[查询结果] --> H[生成提示词构建]
H --> I[RAG上下文准备]
I --> J[自然语言生成]
J --> K[最终回复]
F --> L[结构化查询]
L --> M[数据库查询]
M --> G
```

**图表来源**
- [prompts.py:46-171](file://server/prompts.py#L46-L171)
- [prompts.py:186-218](file://server/prompts.py#L186-L218)

**章节来源**
- [llm_client.py:41-132](file://server/llm_client.py#L41-L132)
- [schemas.py:79-92](file://server/schemas.py#L79-L92)

## 架构概览

Laplace采用双阶段AI处理架构，实现了从自然语言到精确查询的转换：

```mermaid
sequenceDiagram
participant U as 用户
participant API as FastAPI服务
participant LLM as LLM客户端
participant DB as 数据库
participant GEN as 生成器
U->>API : /api/chat 请求
API->>LLM : 第一阶段：意图解析
LLM->>LLM : 结构化输出(JSON模式)
LLM-->>API : 解析后的查询条件
API->>DB : 执行查询
DB-->>API : 查询结果
API->>GEN : 第二阶段：自然语言生成
GEN->>GEN : RAG上下文构建
GEN-->>API : 自然语言回复
API-->>U : 最终响应
Note over API,GEN : 双阶段处理确保准确性
```

**图表来源**
- [main.py:150-242](file://server/main.py#L150-L242)
- [main.py:245-355](file://server/main.py#L245-L355)

## 详细组件分析

### LLM客户端实现

#### 多模型支持策略

LLM客户端实现了智能的多模型回退机制：

```mermaid
flowchart TD
A[调用chat_completion] --> B{指定模型?}
B --> |否| C[使用主模型]
B --> |是| D[使用指定模型]
C --> E[尝试结构化输出]
D --> E
E --> F{成功?}
F --> |是| G[返回结果]
F --> |否| H{有回退模型?}
H --> |是| I[尝试下一个模型]
H --> |否| J[抛出异常]
I --> F
```

**图表来源**
- [llm_client.py:66-84](file://server/llm_client.py#L66-L84)
- [llm_client.py:87-132](file://server/llm_client.py#L87-L132)

#### 结构化输出验证机制

系统采用严格的JSON模式验证：

```mermaid
flowchart TD
A[LLM响应] --> B[提取JSON对象]
B --> C{JSON有效?}
C --> |否| D[抛出验证错误]
C --> |是| E[Pydantic模型验证]
E --> F{验证通过?}
F --> |否| G[抛出验证错误]
F --> |是| H[返回解析结果]
```

**图表来源**
- [llm_client.py:176-183](file://server/llm_client.py#L176-L183)
- [llm_client.py:186-219](file://server/llm_client.py#L186-L219)

**章节来源**
- [llm_client.py:41-132](file://server/llm_client.py#L41-L132)
- [llm_client.py:176-219](file://server/llm_client.py#L176-L219)

### 提示词工程设计

#### 系统提示词构建策略

系统提示词采用动态构建方式，包含以下关键要素：

1. **效果分类注入**：从effect_schema.json动态加载55种效果类型
2. **查询规则定义**：明确支持的查询条件和输出格式
3. **名称映射规则**：处理昵称和别名映射
4. **多从者对比支持**：区分单从者和多从者查询场景

#### 生成提示词设计

生成提示词专注于RAG阶段的自然语言生成：

```mermaid
flowchart TD
A[用户问题] --> B[检索结果上下文]
B --> C[生成原则约束]
C --> D[数据驱动回答]
D --> E[格式规范要求]
E --> F[最终自然语言回复]
C1[直接回答问题]
C2[结合全局统计]
C3[禁绝先验知识]
C4[简洁明快]
C5[格式规范]
C6[合理分类]
C --> C1
C --> C2
C --> C3
C --> C4
C --> C5
C --> C6
```

**图表来源**
- [prompts.py:186-218](file://server/prompts.py#L186-L218)

**章节来源**
- [prompts.py:46-171](file://server/prompts.py#L46-L171)
- [prompts.py:186-218](file://server/prompts.py#L186-L218)

### 两阶段AI处理流程

#### 阶段一：意图解析（JSON模式）

```mermaid
sequenceDiagram
participant U as 用户
participant API as FastAPI
participant LLM as LLM客户端
participant SCHEMA as 数据模式
U->>API : 自然语言查询
API->>LLM : 系统提示词 + 用户消息
LLM->>LLM : 结构化输出(JSON模式)
LLM->>SCHEMA : Pydantic验证
SCHEMA-->>LLM : 验证结果
LLM-->>API : 解析后的查询条件
API->>API : 验证意图类型
API-->>U : 结构化查询结果
```

**图表来源**
- [main.py:156-189](file://server/main.py#L156-L189)
- [llm_client.py:176-183](file://server/llm_client.py#L176-L183)

#### 阶段二：自然语言生成（纯文本模式）

```mermaid
sequenceDiagram
participant API as FastAPI
participant DB as 数据库
participant GEN as 生成器
participant LLM as LLM客户端
API->>DB : 执行查询
DB-->>API : 查询结果
API->>GEN : 构建RAG上下文
GEN->>LLM : 生成提示词 + 上下文
LLM->>LLM : 纯文本生成
LLM-->>GEN : 自然语言回复
GEN-->>API : 格式化回复
API-->>API : 错误处理和降级
API-->>API : 记录完整链路
```

**图表来源**
- [main.py:191-242](file://server/main.py#L191-L242)
- [main.py:245-355](file://server/main.py#L245-L355)

**章节来源**
- [main.py:150-242](file://server/main.py#L150-L242)
- [main.py:245-355](file://server/main.py#L245-L355)

### 查询执行器

查询执行器负责将结构化查询条件转换为数据库查询：

```mermaid
flowchart TD
A[查询条件] --> B{多从者对比?}
B --> |是| C[逐个查询]
B --> |否| D[标准查询]
C --> E[去重和排序]
D --> F[条件匹配]
F --> G[结果排序]
E --> H[返回结果]
G --> H
F1[NP充能条件]
F2[稀有度条件]
F3[职阶条件]
F4[名称搜索]
F5[效果筛选]
F6[特性筛选]
F --> F1
F --> F2
F --> F3
F --> F4
F --> F5
F --> F6
```

**图表来源**
- [query_executor.py:53-116](file://server/query_executor.py#L53-L116)
- [query_executor.py:119-299](file://server/query_executor.py#L119-L299)

**章节来源**
- [query_executor.py:53-116](file://server/query_executor.py#L53-L116)
- [query_executor.py:119-299](file://server/query_executor.py#L119-L299)

## 依赖关系分析

### 组件耦合度分析

```mermaid
graph TB
subgraph "核心层"
A[main.py]
B[llm_client.py]
C[schemas.py]
end
subgraph "功能层"
D[prompts.py]
E[query_executor.py]
F[individuality.py]
end
subgraph "基础设施"
G[logger.py]
H[effect_schema.json]
I[nicknames.json]
end
A --> B
A --> D
A --> E
A --> G
B --> C
E --> F
D --> H
E --> I
```

**图表来源**
- [main.py:17-21](file://server/main.py#L17-L21)
- [llm_client.py:22](file://server/llm_client.py#L22)

### 外部依赖管理

系统对外部依赖采用环境变量配置：

| 环境变量 | 默认值 | 用途 |
|---------|--------|------|
| LLM_BASE_URL | https://x.obao.cloud/v1 | LLM服务基础URL |
| LLM_API_KEY | "" | API密钥 |
| LLM_MODEL | claude-sonnet-4-6 | 主模型名称 |
| LLM_FALLBACK_MODELS | Deepseek-V4-Flash,gpt-5.4 | 回退模型列表 |

**章节来源**
- [llm_client.py:27-34](file://server/llm_client.py#L27-L34)
- [main.py:17-21](file://server/main.py#L17-L21)

## 性能考量

### 温度参数调优策略

系统采用保守的温度设置以确保输出稳定性：

- **意图解析阶段**：temperature=0.1，确保结构化输出的一致性
- **生成阶段**：temperature=0.1，保持回答的准确性和一致性

### 模型选择策略

```mermaid
flowchart TD
A[模型选择] --> B{支持结构化输出?}
B --> |是| C[优先使用]
B --> |否| D[标记为不支持]
C --> E{当前模型可用?}
D --> F{有回退模型?}
E --> |是| G[使用当前模型]
E --> |否| H[尝试下一个模型]
F --> |是| I[使用回退模型]
F --> |否| J[抛出异常]
H --> E
```

**图表来源**
- [llm_client.py:66-84](file://server/llm_client.py#L66-L84)
- [llm_client.py:108-132](file://server/llm_client.py#L108-L132)

### 成本控制建议

1. **令牌限制**：max_tokens默认1024，可根据需求调整
2. **模型选择**：优先选择性价比高的模型
3. **缓存策略**：系统已实现提示词缓存和数据库预加载
4. **错误处理**：自动回退机制避免重复调用

## 故障排除指南

### 常见错误类型及处理

```mermaid
flowchart TD
A[LLM调用失败] --> B{模型支持情况}
B --> |结构化输出不支持| C[降级为纯文本模式]
B --> |模型不可用| D[尝试回退模型]
B --> |网络错误| E[重试机制]
C --> F[使用text_fallback]
D --> G[切换到备用模型]
E --> H[指数退避重试]
F --> I[继续处理]
G --> I
H --> I
I --> J[记录错误日志]
J --> K[返回降级响应]
```

**图表来源**
- [llm_client.py:118-132](file://server/llm_client.py#L118-L132)
- [llm_client.py:167-173](file://server/llm_client.py#L167-L173)

### 日志记录和监控

系统提供完整的链路追踪：

```mermaid
flowchart TD
A[请求处理] --> B[意图解析]
B --> C[查询执行]
C --> D[自然语言生成]
D --> E[响应返回]
B1[错误捕获] --> F[错误日志]
C1[错误捕获] --> F
D1[错误捕获] --> F
F --> G[JSONL格式记录]
G --> H[时间戳跟踪]
H --> I[查询条件记录]
I --> J[结果统计]
```

**图表来源**
- [logger.py:38-55](file://server/logger.py#L38-L55)

**章节来源**
- [logger.py:38-55](file://server/logger.py#L38-L55)
- [test_llm_client.py:98-104](file://tests/test_llm_client.py#L98-L104)

## 结论

Laplace项目的AI集成架构展现了现代LLM应用的最佳实践：

### 核心优势

1. **双阶段处理**：确保从自然语言到精确查询的可靠转换
2. **多模型支持**：智能回退机制提高系统可用性
3. **严格验证**：Pydantic模型验证确保输出质量
4. **提示词工程**：精心设计的提示词模板提升理解准确性
5. **性能优化**：合理的温度设置和成本控制策略

### 设计亮点

- **结构化输出**：通过JSON模式确保查询条件的准确性
- **RAG增强**：结合检索结果生成更可信的回答
- **错误处理**：完善的降级策略保证用户体验
- **监控追踪**：完整的日志记录便于问题诊断

### 改进建议

1. **模型监控**：增加模型性能指标监控
2. **缓存优化**：实现查询结果缓存机制
3. **A/B测试**：支持不同提示词版本的对比测试
4. **成本分析**：增加详细的API调用成本统计

该架构为其他领域的AI应用提供了优秀的参考模板，特别是在需要精确查询和可靠输出的场景中。