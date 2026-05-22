/**
 * Laplace Admin — 后台管理前端逻辑
 * 深色主题版，仅保留环境变量 + 配置文件管理
 */

const API = '/api/admin';

// ── 页面状态 ──
let currentConfigFile = null;
let envSource = 'file'; // "file" or "environ"

// ── 初始化 ──
document.addEventListener('DOMContentLoaded', async () => {
    // 检查登录状态
    const res = await fetch(`${API}/me`);
    const data = await res.json();
    if (data.logged_in) {
        const redirect = new URLSearchParams(window.location.search).get('redirect');
        if (redirect && redirect.startsWith('/')) {
            window.location.href = redirect;
            return;
        }
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
            const redirect = new URLSearchParams(window.location.search).get('redirect');
            if (redirect && redirect.startsWith('/')) {
                window.location.href = redirect;
                return;
            }
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
        const editor = document.getElementById('env-editor');
        const hintEl = document.getElementById('env-source-hint');
        const saveBtn = document.getElementById('env-save-btn');

        editor.value = data.content;
        envSource = data.source || 'file';

        if (envSource === 'environ') {
            // 环境变量注入模式：只读
            editor.readOnly = true;
            saveBtn.disabled = true;
            hintEl.textContent = '⚠ 当前为 --env-file 注入模式（无 .env 文件实体），环境变量只读。如需编辑，请在部署时挂载 volume。';
            hintEl.style.display = 'block';
        } else {
            editor.readOnly = false;
            saveBtn.disabled = false;
            hintEl.style.display = 'none';
        }
    } catch (err) {
        setStatus('env-status', '加载失败: ' + err.message, 'error');
    }
}

async function saveEnv() {
    if (envSource === 'environ') {
        setStatus('env-status', '当前为注入模式，无法保存', 'error');
        return;
    }
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

// ── 工具函数 ──

function setStatus(elId, msg, type) {
    const el = document.getElementById(elId);
    el.textContent = msg;
    el.className = 'status-msg' + (type ? ` ${type}` : '');
}