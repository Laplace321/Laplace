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

    // 监控仪表盘
    document.getElementById('monitor-refresh-btn').addEventListener('click', () => {
        loadMonitor();
        countdownSeconds = REFRESH_INTERVAL;
    });
    document.querySelectorAll('.window-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.window-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            loadMonitor(parseInt(btn.dataset.minutes));
            countdownSeconds = REFRESH_INTERVAL;
        });
    });
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

    // 监控 Tab 激活/离开时控制自动刷新
    if (tabName === 'monitor') {
        loadMonitor();
        startAutoRefresh();
    } else {
        stopAutoRefresh();
    }
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

// ── 监控仪表盘 ──

let monitorMinutes = 5;
let monitorTimer = null;
let countdownTimer = null;
let countdownSeconds = 30;
const REFRESH_INTERVAL = 30;

async function loadMonitor(minutes) {
    if (minutes !== undefined) monitorMinutes = minutes;
    try {
        const res = await fetch(`/api/admin/monitor?minutes=${monitorMinutes}`);
        if (res.status === 401) { handleLogout(); return; }
        const data = await res.json();
        renderMonitor(data);
    } catch (err) {
        document.getElementById('monitor-content').innerHTML =
            `<p class="status-msg error">加载失败: ${err.message}</p>`;
    }
}

function renderMonitor(data) {
    const container = document.getElementById('monitor-content');
    const available = data.model_available || {};
    const llm = data.llm || {};
    const totals = data.totals || {};
    const errorTypes = llm.error_types || {};
    const models = data.models || {};

    let html = '';

    // ── 模型可用性 ──
    html += '<h4 class="monitor-section-title">模型可用性</h4>';
    html += '<div class="model-grid">';
    for (const [model, isUp] of Object.entries(available)) {
        const status = isUp ? 'up' : 'down';
        html += `<div class="model-card ${status}">
            <span class="status-dot ${status}"></span>
            <span class="model-name">${escapeHtml(model)}</span>
        </div>`;
    }
    if (Object.keys(available).length === 0) {
        html += '<p class="monitor-empty">暂无模型数据</p>';
    }
    html += '</div>';

    // ── LLM 调用概览 ──
    html += `<h4 class="monitor-section-title">LLM 调用（${monitorMinutes} 分钟窗口）</h4>`;
    html += '<div class="metric-grid">';
    html += metricCard(llm.calls || 0, '总调用', 'info');
    html += metricCard(llm.successes || 0, '成功', 'success');
    html += metricCard(llm.errors || 0, '失败', llm.errors > 0 ? 'danger' : 'success');
    html += metricCard(llm.fallbacks || 0, '降级', llm.fallbacks > 0 ? 'warning' : 'success');
    html += metricCard((llm.success_rate ?? 100) + '%', '成功率',
        (llm.success_rate ?? 100) >= 90 ? 'success' : (llm.success_rate ?? 100) >= 50 ? 'warning' : 'danger');
    html += metricCard((llm.avg_latency_ms || 0) + 'ms', '平均延迟', 'info');
    html += metricCard((llm.max_latency_ms || 0) + 'ms', '最大延迟', 'info');
    html += '</div>';

    // ── 模型级别统计 ──
    if (Object.keys(models).length > 0) {
        html += '<h4 class="monitor-section-title">模型级别统计</h4>';
        html += '<div class="metric-grid">';
        for (const [model, stats] of Object.entries(models)) {
            const rate = stats.success_rate ?? 100;
            const rateClass = rate >= 90 ? 'success' : rate >= 50 ? 'warning' : 'danger';
            html += `<div class="metric-card">
                <div class="metric-value ${rateClass}" style="font-size:1.1rem">${escapeHtml(model)}</div>
                <div class="metric-label" style="margin-top:0.5rem">
                    ${stats.calls || 0} 调用 · ${rate}% 成功 · ${stats.avg_latency_ms || 0}ms
                </div>
            </div>`;
        }
        html += '</div>';
    }

    // ── 错误类型分布 ──
    html += '<h4 class="monitor-section-title">错误类型分布</h4>';
    if (Object.keys(errorTypes).length > 0) {
        html += '<ul class="error-list">';
        for (const [errType, count] of Object.entries(errorTypes)) {
            html += `<li class="error-tag">
                <span class="error-count">${count}</span> ${escapeHtml(errType)}
            </li>`;
        }
        html += '</ul>';
    } else {
        html += '<p class="monitor-empty">窗口内无错误</p>';
    }

    // ── 历史累计 ──
    html += '<h4 class="monitor-section-title">历史累计（自启动）</h4>';
    html += '<div class="totals-row">';
    html += `<span class="total-item"><strong>${totals.llm_calls || 0}</strong> LLM 调用</span>`;
    html += `<span class="total-item"><strong>${totals.llm_successes || 0}</strong> 成功</span>`;
    html += `<span class="total-item"><strong>${totals.llm_errors || 0}</strong> 失败</span>`;
    html += `<span class="total-item"><strong>${totals.llm_fallbacks || 0}</strong> 降级</span>`;
    html += `<span class="total-item"><strong>${totals.http_requests || 0}</strong> HTTP 请求</span>`;
    html += '</div>';

    // ── 告警历史 ──
    const alertHistory = data.alert_history || [];
    html += '<h4 class="monitor-section-title">告警历史</h4>';
    if (alertHistory.length > 0) {
        html += '<div class="alert-history-list">';
        for (const entry of alertHistory) {
            const levelClass = (entry.level || '').toLowerCase();
            const statusIcon = entry.success ? '✓' : '✗';
            const statusClass = entry.success ? 'sent-ok' : 'sent-fail';
            html += `<div class="alert-history-item">
                <span class="alert-level-tag ${levelClass}">${escapeHtml(entry.level || '?')}</span>
                <span class="alert-time">${escapeHtml(entry.time || '')}</span>
                <span class="alert-title">${escapeHtml(entry.title || '')}</span>
                <span class="alert-channel">${escapeHtml(entry.channel || '')}</span>
                <span class="alert-status ${statusClass}">${statusIcon}</span>
            </div>`;
        }
        html += '</div>';
    } else {
        html += '<p class="monitor-empty">暂无告警记录</p>';
    }

    container.innerHTML = html;
}

function metricCard(value, label, colorClass) {
    return `<div class="metric-card">
        <div class="metric-value ${colorClass}">${value}</div>
        <div class="metric-label">${label}</div>
    </div>`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = String(text);
    return div.innerHTML;
}

function startAutoRefresh() {
    stopAutoRefresh();
    countdownSeconds = REFRESH_INTERVAL;
    updateCountdown();
    countdownTimer = setInterval(() => {
        countdownSeconds--;
        updateCountdown();
        if (countdownSeconds <= 0) {
            loadMonitor();
            countdownSeconds = REFRESH_INTERVAL;
        }
    }, 1000);
}

function stopAutoRefresh() {
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    const el = document.getElementById('refresh-countdown');
    if (el) el.textContent = '';
}

function updateCountdown() {
    const el = document.getElementById('refresh-countdown');
    if (el) el.textContent = countdownSeconds + 's';
}

// ── 工具函数 ──

function setStatus(elId, msg, type) {
    const el = document.getElementById(elId);
    el.textContent = msg;
    el.className = 'status-msg' + (type ? ` ${type}` : '');
}