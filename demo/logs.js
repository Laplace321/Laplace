/**
 * Laplace — Log Viewer
 * 日志查看页面交互逻辑
 */

const API_BASE = "/api/admin/logs";
const PAGE_SIZE = 50;

let currentOffset = 0;
let currentTotal = 0;
let currentKeyword = "";

// === DOM Refs ===
const searchInput = document.getElementById("search-input");
const searchBtn = document.getElementById("search-btn");
const clearSearchBtn = document.getElementById("clear-search-btn");
const searchStats = document.getElementById("search-stats");
const logTbody = document.getElementById("log-tbody");
const emptyState = document.getElementById("empty-state");
const loadingState = document.getElementById("loading-state");
const logTable = document.getElementById("log-table");
const prevBtn = document.getElementById("prev-btn");
const nextBtn = document.getElementById("next-btn");
const pageInfo = document.getElementById("page-info");
const detailModal = document.getElementById("detail-modal");
const detailTraceId = document.getElementById("detail-trace-id");
const detailBody = document.getElementById("detail-body");

// === Phase Display Names ===
const PHASE_NAMES = {
  routing_input: "路由输入",
  routing_output: "路由结果",
  execution: "Skill 执行",
  context_build: "Context 构建",
  generation_input: "生成输入",
  generation_output: "生成输出",
  final: "请求完成",
};

// === Fetch Logs ===
async function fetchLogs() {
  loadingState.classList.remove("hidden");
  emptyState.classList.add("hidden");
  logTbody.innerHTML = "";

  try {
    const params = new URLSearchParams({
      limit: PAGE_SIZE,
      offset: currentOffset,
    });
    if (currentKeyword) params.set("keyword", currentKeyword);

    const resp = await fetch(`${API_BASE}?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    currentTotal = data.total || 0;
    renderLogs(data.items || []);
    updatePagination();
    updateStats();
  } catch (err) {
    logTbody.innerHTML = "";
    emptyState.textContent = `加载失败: ${err.message}`;
    emptyState.classList.remove("hidden");
  } finally {
    loadingState.classList.add("hidden");
  }
}

// === Render Log Rows ===
function renderLogs(items) {
  if (items.length === 0) {
    emptyState.textContent = currentKeyword ? "未找到匹配的日志" : "暂无日志数据";
    emptyState.classList.remove("hidden");
    logTable.classList.add("hidden");
    return;
  }

  logTable.classList.remove("hidden");
  logTbody.innerHTML = items.map(item => {
    const time = formatTime(item.timestamp);
    const statusClass = getStatusClass(item.status);
    const statusLabel = getStatusLabel(item.status);
    const duration = item.duration_ms != null ? `${item.duration_ms.toFixed(0)}ms` : "-";
    const query = escapeHtml(item.query || "(无)");

    return `<tr data-trace-id="${escapeHtml(item.traceId)}">
      <td class="time-cell">${time}</td>
      <td><span class="trace-id">${escapeHtml(item.traceId)}</span></td>
      <td><span class="query-text" title="${query}">${query}</span></td>
      <td style="text-align:center"><span class="status-badge ${statusClass}">${statusLabel}</span></td>
      <td style="text-align:right"><span class="duration">${duration}</span></td>
    </tr>`;
  }).join("");
}

function formatTime(ts) {
  if (!ts) return "-";
  try {
    const d = new Date(ts);
    const pad = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  } catch {
    return ts;
  }
}

function getStatusClass(status) {
  if (status === "success") return "status-success";
  if (status === "error" || status === "routing_error") return "status-error";
  if (status === "fallback" || status === "no_match") return "status-fallback";
  return "status-unknown";
}

function getStatusLabel(status) {
  const map = {
    success: "成功",
    error: "错误",
    routing_error: "路由错误",
    fallback: "降级",
    no_match: "无匹配",
  };
  return map[status] || status;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// === Pagination ===
function updatePagination() {
  const totalPages = Math.max(1, Math.ceil(currentTotal / PAGE_SIZE));
  const currentPage = Math.floor(currentOffset / PAGE_SIZE) + 1;

  prevBtn.disabled = currentOffset <= 0;
  nextBtn.disabled = currentOffset + PAGE_SIZE >= currentTotal;
  pageInfo.textContent = `第 ${currentPage} / ${totalPages} 页`;
}

function updateStats() {
  if (currentKeyword) {
    searchStats.textContent = `搜索 "${currentKeyword}" — 共 ${currentTotal} 条结果`;
  } else {
    searchStats.textContent = `共 ${currentTotal} 条日志`;
  }
}

// === Detail Modal ===
async function showDetail(traceId) {
  detailTraceId.textContent = traceId;
  detailBody.innerHTML = "<p style='color:var(--text-muted)'>加载中...</p>";
  detailModal.classList.remove("hidden");

  try {
    const resp = await fetch(`${API_BASE}/${encodeURIComponent(traceId)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderDetail(data);
  } catch (err) {
    detailBody.innerHTML = `<p style="color:var(--accent-red)">加载失败: ${escapeHtml(err.message)}</p>`;
  }
}

function renderDetail(data) {
  const phases = data.phases || [];

  if (phases.length === 0) {
    // 旧模式：直接展示整个 JSON
    detailBody.innerHTML = `<div class="phase-block expanded">
      <div class="phase-header"><span class="phase-name">完整日志</span></div>
      <div class="phase-body"><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></div>
    </div>`;
    return;
  }

  detailBody.innerHTML = phases.map((phase, i) => {
    const name = PHASE_NAMES[phase.phase] || phase.phase;
    const isError = phase.level === "ERROR" || phase.error;
    const errorClass = isError ? "phase-error" : "";
    const expanded = i === 0 ? "expanded" : "";
    const time = phase.timestamp ? formatTime(phase.timestamp) : "";

    let content = "";
    if (phase.error) {
      content += `<div style="color:var(--accent-red);margin-bottom:8px;font-size:13px">Error: ${escapeHtml(phase.error)}</div>`;
    }
    content += `<pre>${escapeHtml(JSON.stringify(phase.data || {}, null, 2))}</pre>`;

    return `<div class="phase-block ${errorClass} ${expanded}">
      <div class="phase-header">
        <span class="phase-name">${escapeHtml(name)}</span>
        <span class="phase-time">${time}</span>
        <span class="phase-toggle">▶</span>
      </div>
      <div class="phase-body">${content}</div>
    </div>`;
  }).join("");
}

function closeModal() {
  detailModal.classList.add("hidden");
  detailBody.innerHTML = "";
}

// === Event Listeners ===

// Search
searchBtn.addEventListener("click", () => {
  currentKeyword = searchInput.value.trim();
  currentOffset = 0;
  fetchLogs();
});

searchInput.addEventListener("keydown", e => {
  if (e.key === "Enter") {
    currentKeyword = searchInput.value.trim();
    currentOffset = 0;
    fetchLogs();
  }
});

clearSearchBtn.addEventListener("click", () => {
  searchInput.value = "";
  currentKeyword = "";
  currentOffset = 0;
  fetchLogs();
});

// Pagination
prevBtn.addEventListener("click", () => {
  currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
  fetchLogs();
});

nextBtn.addEventListener("click", () => {
  currentOffset += PAGE_SIZE;
  fetchLogs();
});

// Row click → detail
logTbody.addEventListener("click", e => {
  const row = e.target.closest("tr[data-trace-id]");
  if (row) showDetail(row.dataset.traceId);
});

// Modal close
detailModal.querySelector(".modal-close").addEventListener("click", closeModal);
detailModal.querySelector(".modal-overlay").addEventListener("click", closeModal);
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !detailModal.classList.contains("hidden")) closeModal();
});

// Phase toggle
detailBody.addEventListener("click", e => {
  const header = e.target.closest(".phase-header");
  if (header) header.parentElement.classList.toggle("expanded");
});

// === Init ===
fetchLogs();
