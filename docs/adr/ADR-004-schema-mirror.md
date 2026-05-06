# ADR-004: Schema Mirror — 知识提取而非代码复制

- **日期**: 2026-05-05
- **状态**: 已采纳

## 背景
Chaldea 用 Dart 构建了 FGO 最完整的数据类型系统（165+ FuncType、200+ BuffType、40+ SkillEffect 效果分类），需要决定如何利用。

## 决策
不直接翻译 Dart 代码为 Python，而是提取 Chaldea 的「领域知识」（效果分类体系、枚举映射、数据路径约定）生成 JSON 知识库，注入 LLM System Prompt。

## 理由
1. Chaldea Dart 模型高度耦合 Flutter UI（路由、翻译、渲染），直接翻译代价高
2. 知识提取方式维护成本低，Chaldea 更新时只需重新提取枚举
3. LLM 具备知识后可自动处理新的查询类型，不需要每种效果都写查询逻辑
