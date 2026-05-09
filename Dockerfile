# ── Laplace Docker Image ──────────────────────────────
# 基于 python:3.12-slim，单阶段构建（项目无编译依赖）
# 构建: docker build -t laplace .
# 运行: docker run -d --env-file .env -p 8000:8000 -v laplace-logs:/app/server/logs laplace

FROM python:3.12-slim

LABEL maintainer="Laplace <tongchong.tong@cainiao.com>"
LABEL description="AI Native FGO Data Assistant"

# 避免 Python 写 .pyc 和缓冲 stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ── 依赖层（利用 Docker 缓存，依赖不变时跳过） ──
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com -r server/requirements.txt

# ── 应用代码 ──
COPY server/ server/
COPY demo/ demo/

# ── 入口脚本 ──
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

# 确保日志和数据目录存在
RUN mkdir -p server/logs server/data

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
