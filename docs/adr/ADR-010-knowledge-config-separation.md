# ADR-010: 知识与配置物理分离

- **日期**: 2026-05-06
- **状态**: 已采纳（Phase 5 P1）

## 背景
`main.py` 硬编码 `CLASS_MAP`、`NP_CARD_MAP`，`prompts.py` 硬编码效果别名，与 `knowledge/` 中的知识库可能不同步。

## 决策
1. `knowledge/` 存放稳定领域知识（sync_chaldea.py 生成）
2. 新建 `config/` 存放可运营配置（昵称、术语映射、展示规则）
3. 严禁在代码中硬编码翻译字典，必须从 config 加载

## 理由
知识更新与配置维护解耦，支持运营团队独立修改配置。
