#!/bin/sh
set -e

echo "=== Laplace Container Starting ==="

# ── 首次启动：下载从者数据 ──
if [ ! -f "server/data/servants_db.json" ]; then
    echo "[init] servants_db.json not found, downloading from Atlas Academy..."
    python3 -m server.data_loader
    echo "[init] Data download complete."
else
    echo "[init] servants_db.json exists, skipping download."
fi

# ── 可选：启动时刷新数据 ──
if [ "${REFRESH_DATA_ON_START}" = "1" ]; then
    echo "[init] REFRESH_DATA_ON_START=1, re-downloading servant data..."
    python3 -m server.data_loader
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
