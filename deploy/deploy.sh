#!/bin/bash
# Laplace 生产环境部署脚本
# 用途: 在 Debian 13.4 服务器上自动化部署 Laplace 项目

set -e  # 遇到错误立即退出

echo "========================================="
echo " Laplace 生产环境部署脚本"
echo "========================================="

# 配置变量
DOMAIN="laplace-fgo.wuji321.com"
BACKEND_PORT=8000
PROJECT_DIR="/opt/laplace"

# ==========================================
# Step 1: 系统更新和安装依赖
# ==========================================
echo ""
echo "[1/8] 更新系统并安装依赖..."
apt update
apt install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    nginx \
    curl \
    wget

# ==========================================
# Step 2: 安装 Docker
# ==========================================
echo ""
echo "[2/8] 安装 Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | bash
    systemctl enable docker
    systemctl start docker
    echo "✅ Docker 安装完成"
else
    echo "️  Docker 已安装,跳过"
fi

# ==========================================
# Step 3: 克隆项目代码
# ==========================================
echo ""
echo "[3/8] 克隆项目代码..."
if [ -d "$PROJECT_DIR" ]; then
    echo "ℹ️  项目目录已存在,更新代码..."
    cd "$PROJECT_DIR"
    git pull origin main
else
    echo "克隆项目到 $PROJECT_DIR..."
    git clone https://github.com/Laplace321/Laplace.git "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

# ==========================================
# Step 4: 配置环境变量
# ==========================================
echo ""
echo "[4/8] 配置环境变量..."
cd "$PROJECT_DIR"

if [ ! -f .env ]; then
    echo "创建 .env 文件..."
    cp .env.example .env
    
    echo ""
    echo "⚠️  请编辑 .env 文件并填入以下信息:"
    echo "   1. LLM_API_KEY (你的 LLM API 密钥)"
    echo "   2. CORS_ORIGINS=https://$DOMAIN"
    echo ""
    echo "按 Enter 继续,或 Ctrl+C 退出编辑..."
    read
    
    nano .env
else
    echo "ℹ️  .env 文件已存在"
fi

# ==========================================
# Step 5: 构建 Docker 镜像
# ==========================================
echo ""
echo "[5/8] 构建 Docker 镜像..."
docker build -t laplace:latest .

# ==========================================
# Step 6: 停止旧容器 (如果有)
# ==========================================
echo ""
echo "[6/8] 停止旧容器..."
if docker ps -a | grep -q laplace; then
    docker stop laplace
    docker rm laplace
    echo "✅ 旧容器已停止并删除"
fi

# ==========================================
# Step 7: 启动新容器
# ==========================================
echo ""
echo "[7/8] 启动 Laplace 容器..."
docker run -d \
    --name laplace \
    --env-file .env \
    -p $BACKEND_PORT:8000 \
    -v laplace-logs:/app/server/logs \
    --restart unless-stopped \
    laplace:latest

echo "✅ 容器已启动"
sleep 3

# 检查容器状态
if docker ps | grep -q laplace; then
    echo "✅ 容器运行正常"
else
    echo "❌ 容器启动失败,查看日志:"
    docker logs laplace
    exit 1
fi

# ==========================================
# Step 8: 配置 Nginx 反向代理
# ==========================================
echo ""
echo "[8/8] 配置 Nginx 反向代理..."

# 创建 Nginx 配置
cat > /etc/nginx/sites-available/laplace << 'EOF'
# Laplace Nginx 配置
# 域名: laplace-fgo.wuji321.com

server {
    listen 80;
    server_name laplace-fgo.wuji321.com;

    # 前端静态文件
    root /opt/laplace/demo;
    index index.html;

    # 前端路由 (SPA)
    location / {
        try_files $uri $uri/ /index.html;
        
        # CORS 配置
        add_header Access-Control-Allow-Origin *;
        add_header Access-Control-Allow-Methods 'GET, POST, OPTIONS';
        add_header Access-Control-Allow-Headers 'DNT,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Range';
    }

    # API 反向代理
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        
        # SSE 流式响应配置 (重要!)
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Connection '';
        chunked_transfer_encoding off;
        
        # 标准代理头
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        # 超时配置
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }

    # 日志
    access_log /var/log/nginx/laplace-access.log;
    error_log /var/log/nginx/laplace-error.log;
}
EOF

# 启用配置
ln -sf /etc/nginx/sites-available/laplace /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# 测试配置
nginx -t

# 重启 Nginx
systemctl restart nginx

echo "✅ Nginx 配置完成"

# ==========================================
# 部署完成
# ==========================================
echo ""
echo "========================================="
echo "✨ 部署完成!"
echo "========================================="
echo ""
echo "访问地址:"
echo "  前端: http://$DOMAIN"
echo "  API:  http://$DOMAIN/api/*"
echo ""
echo "管理命令:"
echo "  查看日志:   docker logs -f laplace"
echo "  重启服务:   docker restart laplace"
echo "  停止服务:   docker stop laplace"
echo "  更新代码:   cd $PROJECT_DIR && git pull && docker build -t laplace . && docker restart laplace"
echo ""
echo "下一步:"
echo "  1. 在 Cloudflare 配置 DNS 解析: $DOMAIN → 47.239.178.73"
echo "  2. 启用 Cloudflare CDN 和 SSL (Flexible 或 Full 模式)"
echo "  3. 测试访问: http://$DOMAIN"
echo ""
echo "========================================="
