# ADR-001: FGO NP 数据精度约定

- **日期**: 2026-05-05
- **状态**: 已采纳

## 背景
FGO 数据中 NP 值以万分之一为单位存储。

## 决策
`svals[].Value = 3000` 表示 30%，精度 = Value / 100。

## 理由
与 Atlas Academy API 和 Chaldea 源码保持一致。
