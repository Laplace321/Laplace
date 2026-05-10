# Laplace 生产环境部署指南

本指南详细说明如何在云服务器上部署 Laplace 项目,并配置 CDN 和 SSL。

## 前置条件

- 云服务器: Debian 13.4 或 Ubuntu 22.04+
- 公网 IP: `<你的服务器IP>`
- 域名: `<你的域名>`
- Cloudflare 账号 (或其他 DNS 服务商)

---

## Step 1: SSH 密钥配置 (推荐)

### 1.1 检查本地 SSH 密钥

```bash
# 在本地终端执行
ls -la ~/.ssh/*.pub
```

如果看到 `id_ed25519.pub` 或 `id_rsa.pub`,说明已有密钥。

### 1.2 复制公钥到服务器

```bash
# 方法 A: 使用 ssh-copy-id (推荐)
ssh-copy-id root@<你的服务器IP>

# 方法 B: 手动复制
cat ~/.ssh/id_ed25519.pub
# 复制输出内容,然后:
ssh root@<你的服务器IP>
mkdir -p ~/.ssh
echo "你的公钥内容" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
chmod 700 ~/.ssh
```

### 1.3 测试密钥登录

```bash
ssh root@<你的服务器IP>
# 应该无需密码直接登录
```

### 1.4 (可选) 禁用密码登录

```bash
# SSH 到服务器后执行
sudo nano /etc/ssh/sshd_config

# 修改以下配置:
PasswordAuthentication no
PubkeyAuthentication yes

# 重启 SSH 服务
sudo systemctl restart sshd
```

---

## Step 2: 上传并执行部署脚本

### 2.1 上传部署脚本

```bash
# 在本地终端执行
cd /path/to/laplace
scp deploy/deploy.example.sh root@<你的服务器IP>:/root/deploy.sh
```

### 2.2 SSH 到服务器并配置

```bash
ssh root@<你的服务器IP>

# 编辑部署脚本
cd /root
nano deploy.sh

# 修改以下变量:
DOMAIN="<你的域名>"
BACKEND_PORT=8000
PROJECT_DIR="/opt/laplace"
```

### 2.3 执行部署

```bash
chmod +x deploy.sh
./deploy.sh
```

部署脚本会自动完成:
- ✅ 安装 Docker
- ✅ 克隆项目代码
- ✅ 构建 Docker 镜像
- ✅ 配置 Nginx 反向代理
- ✅ 启动服务

### 2.4 配置 .env 文件

部署过程中会提示编辑 `.env` 文件:

```bash
nano /opt/laplace/.env

# 必须配置:
LLM_API_KEY=<你的LLM API密钥>
CORS_ORIGINS=https://<你的域名>

# 可选配置:
RATE_LIMIT_PER_MINUTE=10
RATE_LIMIT_GLOBAL_PER_MINUTE=100
```

### 2.5 验证部署

```bash
# 检查容器状态
docker ps

# 查看日志
docker logs -f laplace

# 测试后端 API
curl http://localhost:8000/docs

# 测试 Nginx
curl http://localhost
```

---

## Step 3: DNS 配置

### 3.1 添加 DNS 记录

在你的 DNS 服务商处添加 A 记录:

| 字段 | 值 |
|:---|:---|
| **Type** | `A` |
| **Name** | `<子域名,如 laplace>` |
| **Content** | `<你的服务器IP>` |
| **TTL** | `Automatic` 或 `300` |

### 3.2 验证 DNS 解析

```bash
# 在本地终端执行
nslookup <你的域名>
ping <你的域名>

# 应该看到解析到你的服务器 IP
```

---

## Step 4: SSL 证书配置

### 4.1 方案 A: 使用 Cloudflare (推荐)

1. 在 Cloudflare 添加你的域名
2. 进入 **SSL/TLS** → **Overview**
3. 选择加密模式:

| 模式 | 适用场景 | 推荐 |
|:---|:---|:---|
| **Flexible** | 客户端→Cloudflare 加密,Cloudflare→服务器不加密 | ✅ 简单快速 |
| **Full** | 全程加密,服务器需自签名证书 | ✅ 更安全 |
| **Full (Strict)** | 全程加密,服务器需有效证书 | ⭐ 最安全 |

**推荐配置**:
- 初期测试: `Flexible` (无需服务器证书)
- 生产环境: `Full` 或 `Full (Strict)`

### 4.2 方案 B: Let's Encrypt (免费证书)

```bash
# 安装 Certbot
sudo apt install -y certbot python3-certbot-nginx

# 获取证书
sudo certbot --nginx -d <你的域名>

# 自动续期
sudo certbot renew --dry-run
```

### 4.3 方案 C: 自签名证书 (仅测试)

```bash
# 生成自签名证书
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/laplace.key \
  -out /etc/ssl/certs/laplace.crt \
  -subj "/CN=<你的域名>"

# 设置权限
sudo chmod 600 /etc/ssl/private/laplace.key
sudo chmod 644 /etc/ssl/certs/laplace.crt
```

---

## Step 5: Nginx 配置优化

### 5.1 HTTPS 配置

如果使用 SSL 证书,修改 Nginx 配置:

```bash
sudo nano /etc/nginx/sites-available/laplace

# 添加 HTTPS server 块:
server {
    listen 443 ssl;
    server_name <你的域名>;
    
    ssl_certificate /path/to/cert.crt;
    ssl_certificate_key /path/to/key.key;
    
    # SSL 优化配置
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    
    # ... 其他配置保持不变
}

# HTTP 重定向到 HTTPS
server {
    listen 80;
    server_name <你的域名>;
    return 301 https://$server_name$request_uri;
}
```

### 5.2 测试并重载 Nginx

```bash
sudo nginx -t
sudo systemctl reload nginx
```

---

## Step 6: CDN 优化 (可选)

### 6.1 缓存配置

在 CDN 服务商配置:
- **Caching Level**: `Standard`
- **Browser Cache TTL**: `2 hours`

### 6.2 页面规则

添加规则:

**规则 1: API 不缓存**
- URL: `<你的域名>/api/*`
- Settings:
  - Cache Level: `Bypass`

**规则 2: 静态资源长期缓存**
- URL: `<你的域名>/*.css`
- Settings:
  - Cache Level: `Cache Everything`
  - Edge Cache TTL: `1 month`

---

## Step 7: 测试和验证

### 7.1 DNS 传播检查

```bash
# 等待 5-10 分钟让 DNS 生效
dig <你的域名>
```

### 7.2 访问测试

```bash
# HTTP 访问
curl -I http://<你的域名>

# HTTPS 访问 (配置 SSL 后)
curl -I https://<你的域名>

# API 测试
curl https://<你的域名>/api/docs
```

### 7.3 浏览器测试

1. 访问: `https://<你的域名>`
2. 测试查询功能
3. 检查浏览器控制台是否有错误

---

## Step 8: 日常维护

### 8.1 查看日志

```bash
# 应用日志
docker logs -f laplace

# Nginx 日志
sudo tail -f /var/log/nginx/laplace-access.log
sudo tail -f /var/log/nginx/laplace-error.log

# Docker 日志
docker logs --tail 100 laplace
```

### 8.2 更新代码

```bash
cd /opt/laplace
git pull origin main
docker build -t laplace:latest .
docker restart laplace
```

### 8.3 监控服务

```bash
# 检查容器状态
docker ps

# 检查磁盘空间
df -h

# 检查内存使用
free -h

# 检查 CPU 使用
top
```

---

## 常见问题排查

### 问题 1: 502 Bad Gateway

**原因**: Nginx 无法连接到后端

**解决**:
```bash
# 检查后端是否运行
docker ps | grep laplace

# 检查后端日志
docker logs laplace

# 重启后端
docker restart laplace
```

### 问题 2: CORS 错误

**原因**: 跨域配置不正确

**解决**:
```bash
# 检查 .env 配置
cat /opt/laplace/.env | grep CORS

# 应该是:
CORS_ORIGINS=https://<你的域名>

# 重启服务
docker restart laplace
```

### 问题 3: SSL 证书错误

**原因**: 证书配置问题

**解决**:
```bash
# 检查 Nginx SSL 配置
sudo nginx -t

# 检查证书文件
ls -la /path/to/cert.crt
ls -la /path/to/key.key
```

### 问题 4: LLM API 返回 403 (Cloudflare 拦截)

**原因**: obao API 前端有 Cloudflare 防护,服务器直接 HTTP 请求会被当作 Bot 拦截,返回 403 JS Challenge 页面。

**根因**: Cloudflare 基于 User-Agent 进行 Bot 检测。Python httpx / curl 的默认 User-Agent 被识别为 Bot。

**解决**: 使用 Nginx 反向代理,注入浏览器 User-Agent 绕过 Bot 检测:

```bash
# 1. 确认 Nginx 配置中包含 /llm-proxy/ location block
cat /etc/nginx/sites-available/laplace | grep -A 15 "llm-proxy"

# 2. 验证反代生效（期望返回 401,而非 403）
curl -s -o /dev/null -w "HTTP %{http_code}" \
  -H "Authorization: Bearer test" \
  http://127.0.0.1/llm-proxy/v1/models

# 3. 确认 .env 的 LLM_BASE_URL 指向反代
grep LLM_BASE_URL /opt/laplace/.env
# 应为: LLM_BASE_URL=http://172.17.0.1/llm-proxy/v1

# 4. 重启容器使配置生效
docker restart laplace
```

**Nginx 反代配置参考** (在 80 和 443 server block 中均需添加):

```nginx
location /llm-proxy/ {
    # 强制 IPv4 解析,避免 IPv6 不可达导致间歇性 502/503
    resolver 8.8.8.8 ipv6=off;
    set $llm_backend https://api.obao.cloud;
    # 使用 rewrite 剥离 /llm-proxy/ 前缀,正确映射到上游路径
    rewrite ^/llm-proxy/(.*)$ /$1 break;
    proxy_pass $llm_backend;
    proxy_ssl_server_name on;
    proxy_set_header Host api.obao.cloud;
    proxy_set_header User-Agent "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36";
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Content-Type $http_content_type;
    proxy_buffering off;
    proxy_read_timeout 120s;
    proxy_connect_timeout 10s;
    allow 127.0.0.1;
    allow 172.17.0.0/16;
    deny all;
}
```

> **注意事项**:
> - `resolver ... ipv6=off` 是必须的,因为服务器 IPv6 不可达,Nginx 默认会先尝试 IPv6 导致间歇性失败。
> - 使用 `set $llm_backend` + `rewrite` 而非直接 `proxy_pass https://api.obao.cloud/`,是因为 Nginx 使用变量时需要 resolver,同时 rewrite 确保路径正确剥离 `/llm-proxy/` 前缀。
> - 此方案依赖 Cloudflare 当前的 User-Agent 检测策略。如未来 Cloudflare 升级检测机制导致再次 403,需重新评估绕过方案。

### 问题 5: DNS 未生效

**原因**: DNS 传播延迟

**解决**:
```bash
# 清除本地 DNS 缓存
sudo dscacheutil -flushcache  # macOS
ipconfig /flushdns            # Windows

# 使用公共 DNS 测试
dig @8.8.8.8 <你的域名>
```

---

## 快速参考

### 重要命令

```bash
# SSH 登录
ssh root@<你的服务器IP>

# 查看服务状态
docker ps
systemctl status nginx

# 重启服务
docker restart laplace
systemctl restart nginx

# 查看日志
docker logs -f laplace
tail -f /var/log/nginx/laplace-access.log

# 更新部署
cd /opt/laplace && git pull && docker build -t laplace . && docker restart laplace
```

### 重要文件

```
/opt/laplace/.env                          # 环境变量配置
/etc/nginx/sites-available/laplace         # Nginx 配置
/etc/ssl/certs/<cert>.crt                  # SSL 证书
/etc/ssl/private/<key>.key                 # SSL 私钥
/var/log/nginx/laplace-*.log               # Nginx 日志
```

### 重要端口

```
80   - HTTP (Nginx)
443  - HTTPS (Nginx)
8000 - FastAPI 后端 (内部)
```

---

## 安全检查清单

- [ ] 使用 SSH 密钥登录,禁用密码登录
- [ ] 配置防火墙,只开放必要端口 (80, 443)
- [ ] 定期更新系统和 Docker 镜像
- [ ] 配置日志轮转,避免磁盘占满
- [ ] 设置监控告警 (可选)
- [ ] 定期备份重要数据 (可选)
- [ ] **不要将敏感信息提交到 Git**

---

## 下一步

- [ ] 配置监控告警 (可选)
- [ ] 设置自动备份 (可选)
- [ ] 优化性能调优 (可选)
- [ ] 添加用户认证 (可选)

---

**文档版本**: v1.1  
**最后更新**: 2026-05-06  
**维护者**: Laplace Team
