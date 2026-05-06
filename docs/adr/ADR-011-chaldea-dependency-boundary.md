# ADR-011: Chaldea 依赖边界明确化

- **日期**: 2026-05-06
- **状态**: 已采纳

## 背景
新人容易误解 `chaldea-center/chaldea` 是 runtime 强依赖。

## 决策
1. 明确仅 `sync_chaldea.py` 更新领域知识时需要 Chaldea 源码
2. 普通运行只依赖 `knowledge/*.json` 和 `servants_db.json`
3. 支持 `CHALDEA_SRC_PATH` 环境变量指定源码路径
4. Chaldea 源码从 https://github.com/chaldea-center/chaldea.git 拉取

## 理由
降低部署门槛，避免不必要的 git submodule 或 clone 操作。
