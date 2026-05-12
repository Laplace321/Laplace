#!/bin/sh
set -e

echo "=== Laplace Container Starting ==="

# ── 从挂载的 .env 文件加载环境变量（volume 挂载模式） ──
if [ -f "/app/.env" ]; then
    echo "[init] Loading .env from mounted volume..."
    set -a
    . /app/.env
    set +a
fi

# ── 数据初始化（版本戳机制：代码更新时自动重建） ──
NEED_REBUILD=0
BUILD_VER=$(cat /app/.build_version 2>/dev/null || echo "unknown")
DATA_VER=$(cat server/data/.data_build_version 2>/dev/null || echo "")

if [ ! -f "server/data/servants_db.json" ]; then
    echo "[init] servants_db.json not found, will build..."
    NEED_REBUILD=1
elif [ "$BUILD_VER" != "$DATA_VER" ]; then
    echo "[init] Build version changed ($DATA_VER -> $BUILD_VER), rebuilding data..."
    NEED_REBUILD=1
else
    echo "[init] servants_db.json up-to-date (version: $DATA_VER), skipping."
fi

# REFRESH_DATA_ON_START=1 可手动强制刷新
if [ "$NEED_REBUILD" = "1" ] || [ "${REFRESH_DATA_ON_START}" = "1" ]; then
    python3 -m server.data_loader
    echo "$BUILD_VER" > server/data/.data_build_version
    echo "[init] Data build complete (version: $BUILD_VER)."
fi

# ── 持久化应用日志（防止 docker rm 后丢失） ──
APP_LOG_DIR="server/logs"
APP_LOG_FILE="${APP_LOG_DIR}/app.log"
mkdir -p "${APP_LOG_DIR}"

{
    echo ""
    echo "======== Container Start: $(date '+%Y-%m-%d %H:%M:%S %Z') ========"
} >> "${APP_LOG_FILE}"

echo "[start] Launching uvicorn on 0.0.0.0:8000 ..."
echo "[start] App log persisted to: ${APP_LOG_FILE}"

exec python3 -m uvicorn server.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-1}" \
    --timeout-keep-alive 75 \
    --log-config server/config/uvicorn_log_config.json
