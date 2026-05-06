# ADR-002: 数据获取方式

- **日期**: 2026-05-05
- **状态**: 已采纳

## 背景
需要获取全量从者技能数据。

## 决策
使用 Atlas Academy 的批量导出端点 `nice_servant_lang_en.json`，Python 预处理后生成静态 JSON。

## 理由
避免前端频繁调 API，数据可离线使用。
