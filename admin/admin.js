/**
 * Laplace Admin — 后台管理前端逻辑
 */

const API = '/api/admin';

// ── 页面状态 ──
let currentConfigFile = null;
let logsPage = 0;
const LOGS_PER_PAGE = 50;

// ── 初始化 ──
document.addEventListener('DOMContentLoaded', async () => {
    // 检查登录状态
    const res = await fetch(`${API}/me`);
    const data = await res.json();
    if (data.logged_in) {
        showAdminPage();
    }

    // 绑定事件
    document.getElementById('login-form').addEventListener('submit', handleLogin);
    document.getElementById('logout-btn').addEventListener('click', handleLogout);

    // Tab 切换
    document.querySelectorAll('.tab').forEach(tab => {
        tab.addEventListener('click', () => switchTab(tab.dataset.tab));
    });

    // 环境变量
    document.getElementById('env-save-btn').addEventListener('click', saveEnv);
    document.getElementById('env-restart-btn').addEventListener('click', restartContainer);

    // 配置文件
    document.getElementById('config-save-btn').addEventListener('click', saveConfig);

    // 日志
    document.getElementById('logs-search-btn').addEventListener('click', () => { logsPage = 0; loadLogs(); });
    document.getElementById('logs-refresh-btn').addEventListener('click', () => { logsPage = 0; loadLogs(); });
    document.getElementById('logs-prev').addEventListener('click', () => { if (logsPage > 0) { logsPage--; loadLogs(); } });
    document.getElementById('logs-next').addEventListener('click', () => { logsPage++; loadLogs(); });
    document.getElementById('logs-keyword').addEventListener('keydown', (e) => { if (e.key === 'Enter') { logsPage = 0; loadLogs(); } });

    // 弹窗关闭
    document.querySelector('.modal-close').addEventListener('click', closeModal);
    document.getElementById('log-detail-modal').addEventListener('click', (e) => { if (e.target === e.currentTarget) closeModal(); });
});

// ── 登录/登出 ──

async function handleLogin(e) {
    e.preventDefault();
    const password = document.getElementById('password-input').value;
    const errorEl = document.getElementById('login-error');
    errorEl.textContent = '';

    try {
        const res = await fetch(`${API}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password }),
        });
        const data = await res.json();
        if (res.ok) {
            showAdminPage();
        } else {
            errorEl.textContent = data.detail || '登录失败';
        }
    } catch (err) {
        errorEl.textContent = '网络错误';
    }
}

async function handleLogout() {
    await fetch(`${API}/logout`, { method: 'POST' });
    document.getElementById('admin-page').style.display = 'none';
    document.getElementById('login-page').style.display = 'flex';
    document.getElementById('password-input').value = '';
}

function showAdminPage() {
    document.getElementById('login-page').style.display = 'none';
    document.getElementById('admin-page').style.display = 'block';
    loadEnv();
    loadConfigList();
    loadLogs();
}

// ── Tab 切换 ──

function switchTab(tabName) {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tabName));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.toggle('active', c.id === `tab-${tabName}`));
}

// ── 环境变量管理 ──

async function loadEnv() {
    try {
        const res = await fetch(`${API}/env`);
        if (res.status === 401) { handleLogout(); return; }
        const data = await res.json();
        document.getElementById('env-editor').value = data.content;
    } catch (err) {
        setStatus('env-status', '加载失败: ' + err.message, 'error');
    }
}

async function saveEnv() {
    const content = document.getElementById('env-editor').value;
    try {
        const res = await fetch(`${API}/env`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });
        const data = await res.json();
        if (res.ok) {
            setStatus('env-status', data.message, 'success');
        } else {
            setStatus('env-status', data.detail || '保存失败', 'error');
        }
    } catch (err) {
        setStatus('env-status', '网络错误', 'error');
    }
}

async function restartContainer() {
    if (!confirm('确定要重启容器吗？服务将短暂中断 2~3 秒。')) return;
    setStatus('env-status', '正在重启...', '');
    try {
        const res = await fetch(`${API}/restart`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            setStatus('env-status', data.message + '（页面将在 5 秒后刷新）', 'success');
            setTimeout(() => location.reload(), 5000);
        } else {
            setStatus('env-status', data.detail || '重启失败', 'error');
        }
    } catch (err) {
        setStatus('env-status', '重启请求已发送，等待容器恢复...', 'success');
        setTimeout(() => location.reload(), 5000);
    }
}

// ── 配置文件管理 ──

async function loadConfigList() {
    try {
        const res = await fetch(`${API}/config`);
        if (res.status === 401) { handleLogout(); return; }
        const data = await res.json();
        const list = document.getElementById('config-list');
        list.innerHTML = '';
        data.configs.forEach(cfg => {
            const li = document.createElement('li');
            li.textContent = cfg.name;
            li.addEventListener('click', () => loadConfig(cfg.name, li));
            list.appendChild(li);
        });
    } catch (err) {
        console.error('加载配置列表失败:', err);
    }
}

async function loadConfig(filename, liEl) {
    document.querySelectorAll('#config-list li').forEach(l => l.classList.remove('active'));
    if (liEl) liEl.classList.add('active');
    currentConfigFile = filename;
    document.getElementById('config-current-file').textContent = filename;
    document.getElementById('config-save-btn').disabled = false;
    document.getElementById('config-editor').disabled = false;

    try {
        const res = await fetch(`${API}/config/${filename}`);
        const data = await res.json();
        // 格式化 JSON 显示
        try {
            const parsed = JSON.parse(data.content);
            document.getElementById('config-editor').value = JSON.stringify(parsed, null, 2);
        } catch {
            document.getElementById('config-editor').value = data.content;
        }
        setStatus('config-status', '', '');
    } catch (err) {
        setStatus('config-status', '加载失败: ' + err.message, 'error');
    }
}

async function saveConfig() {
    if (!currentConfigFile) return;
    const content = document.getElementById('config-editor').value;

    // 前端 JSON 校验
    try {
        JSON.parse(content);
    } catch (e) {
        setStatus('config-status', 'JSON 格式错误: ' + e.message, 'error');
        return;
    }

    try {
        const res = await fetch(`${API}/config/${currentConfigFile}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content }),
        });
        const data = await res.json();
        if (res.ok) {
            setStatus('config-status', data.message, 'success');
        } else {
            setStatus('config-status', data.detail || '保存失败', 'error');
        }
    } catch (err) {
        setStatus('config-status', '网络错误', 'error');
    }
}

// ── 日志查看 ──

async function loadLogs() {
    const keyword = document.getElementById('logs-keyword').value;
    const offset = logsPage * LOGS_PER_PAGE;

    try {
        let url = `${API}/logs?limit=${LOGS_PER_PAGE}&offset=${offset}`;
        if (keyword) url += `&keyword=${encodeURIComponent(keyword)}`;
        const res = await fetch(url);
        if (res.status === 401) { handleLogout(); return; }
        const data = await res.json();
        const traces = data.traces || data;

        const tbody = document.getElementById('logs-tbody');
        tbody.innerHTML = '';

        if (Array.isArray(traces)) {
            traces.forEach(t => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                    <td>${formatTime(t.timestamp)}</td>
                    <td><code>${(t.traceId || '').substring(0, 8)}</code></td>
                    <td>${escapeHtml(t.query || t.message || '-')}</td>
                    <td>${t.mode || '-'}</td>
                    <td>${t.total_tokens || '-'}</td>
                `;
                tr.addEventListener('click', () => showLogDetail(t.traceId));
                tbody.appendChild(tr);
            });
        }

        document.getElementById('logs-prev').disabled = logsPage === 0;
        document.getElementById('logs-page-info').textContent = `第 ${logsPage + 1} 页`;
        document.getElementById('logs-next').disabled = !Array.isArray(traces) || traces.length < LOGS_PER_PAGE;
    } catch (err) {
        console.error('加载日志失败:', err);
    }
}

async function showLogDetail(traceId) {
    if (!traceId) return;
    try {
        const res = await fetch(`${API}/logs/${traceId}`);
        const data = await res.json();
        document.getElementById('log-detail-content').textContent = JSON.stringify(data, null, 2);
        document.getElementById('log-detail-modal').style.display = 'flex';
    } catch (err) {
        alert('加载详情失败: ' + err.message);
    }
}

function closeModal() {
    document.getElementById('log-detail-modal').style.display = 'none';
}

// ── 工具函数 ──

function setStatus(elId, msg, type) {
    const el = document.getElementById(elId);
    el.textContent = msg;
    el.className = 'status-msg' + (type ? ` ${type}` : '');
}

function formatTime(ts) {
    if (!ts) return '-';
    try {
        const d = new Date(ts);
        return d.toLocaleString('zh-CN', { hour12: false });
    } catch { return ts; }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
