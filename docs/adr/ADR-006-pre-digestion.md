# ADR-006: 数据后端预消化 (Pre-digestion)

- **日期**: 2026-05-05
- **状态**: 已采纳

## 背景
LLM 在 RAG 阶段翻译英文枚举（如 `breakAvoidance`）不专业，且浪费 Token。

## 决策
在 Python 组装 JSON 上下文时，强制将职阶、卡色、技能效果、宝具效果翻译为标准中文术语后再投喂给 LLM。

## 理由
根除术语翻译幻觉，精简 Prompt，降低 Token 成本，提升输出专业度。
