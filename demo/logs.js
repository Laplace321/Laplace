/**
 * Laplace — Log Viewer
 * 日志查看页面交互逻辑
 */

const API_BASE = "/api/admin/logs";
const PAGE_SIZE = 50;

let currentOffset = 0;
let currentTotal = 0;
let currentKeyword = "";
let currentRating = "";

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
  agent_detail: "Agent 详情",
  final: "请求完成",
  // 链路 B：Atlas 知识问答
  atlas_search: "Atlas 检索",
  fact_verify: "事实校验",
  // 链路 C：攻略知识问答
  guide_search: "攻略检索",
};

// === Mode Display ===
function getModeLabel(mode) {
  const map = {
    oneshot: "OneShot",
    oneshot_direct: "OneShot",
    oneshot_llm: "OneShot",
    agent_fallback: "Agent",
    fallback_greeting: "问候",
    fallback_out_of_scope: "超范围",
    fallback_no_match: "无匹配",
    routing_error: "错误",
    execution_fallback: "降级",
    // 链路 B：Atlas 知识问答
    atlas_pipeline: "Atlas 知识问答",
    atlas_no_match: "Atlas 无结果",
    // 链路 C：攻略知识问答
    guide_pipeline: "攻略知识问答",
    guide_no_match: "攻略无结果",
  };
  return map[mode] || mode || "-";
}

function getModeClass(mode) {
  if (!mode) return "";
  if (mode.startsWith("oneshot")) return "mode-oneshot";
  if (mode === "agent_fallback") return "mode-agent";
  return "mode-fallback";
}

function getRatingLabel(rating) {
  const map = { good: "优秀", ok: "一般", bad: "糟糕" };
  return map[rating] || "-";
}

function getRatingClass(rating) {
  if (!rating) return "";
  return `rating-${rating}`;
}

function formatTokens(tokens) {
  if (tokens == null || tokens === 0) return "-";
  return tokens.toLocaleString();
}

// === v0.5.1 BI 维度展示 ===
const PIPELINE_LABELS = {
  A: "A 主链路",
  B: "B Atlas",
  C: "C 攻略",
  agent: "Agent 兜底",
  preset: "预设回复",
  confirmation: "二次确认",
  fallback: "降级",
  direct: "直接回复",
  full_a: "A 完整流程",
  sse: "SSE 流式",
  unknown: "未知",
};

const TURN_TYPE_LABELS = {
  MAJOR: "新话题",
  MINOR: "追问",
  CORRECTION: "纠正",
  RESUME: "中断恢复",
  unknown: "未知",
};

const SKILL_DISPLAY_NAMES = {
  search_by_effect: "效果筛选",
  search_by_class: "职阶筛选",
  search_by_trait: "特性筛选",
  search_by_rarity: "星级筛选",
  search_by_npcharge: "充能筛选",
  lookup_servant: "从者查询",
  lookup_ce: "礼装查询",
  respond_servant_detail: "从者详情",
  respond_servant_list: "从者列表",
  respond_recommendation: "推荐回复",
  atlas_search: "Atlas 检索",
  guide_search: "攻略检索",
  fact_verify: "事实校验",
};

function getPipelineLabel(p) {
  if (!p) return "-";
  return PIPELINE_LABELS[p] || p;
}

function getTurnTypeLabel(t) {
  if (!t) return "-";
  return TURN_TYPE_LABELS[t] || t;
}

function formatSkillsCell(names) {
  if (!names) return "-";
  const arr = String(names).split(",").map(s => s.trim()).filter(Boolean);
  if (arr.length === 0) return "-";
  const labels = arr.map(n => SKILL_DISPLAY_NAMES[n] || n);
  if (labels.length <= 2) return escapeHtml(labels.join(" + "));
  // 超过 2 个，前 2 个 + …
  return `<span title="${escapeHtml(labels.join(" + "))}">${escapeHtml(labels.slice(0, 2).join(" + "))}…</span>`;
}

function getErrorReasonLabel(reason) {
  if (!reason) return "-";
  const map = {
    routing_error: "路由错误",
    stream_error: "流式异常",
    preset_stream_error: "预设流异常",
    confirmation_stream_error: "确认流异常",
    execution_error: "执行错误",
    llm_error: "LLM 错误",
    rate_limit: "限流",
    timeout: "超时",
  };
  return map[reason] || reason;
}

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
    if (currentRating) params.set("rating", currentRating);

    const resp = await fetch(`${API_BASE}?${params}`);
    if (resp.status === 401) {
      window.location.href = "/admin/?redirect=/logs.html";
      return;
    }
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

    const modeLabel = getModeLabel(item.mode);
    const modeClass = getModeClass(item.mode);
    const tokens = formatTokens(item.total_tokens);

    const ratingLabel = getRatingLabel(item.rating);
    const ratingClass = getRatingClass(item.rating);

    // v0.5.1 维度
    const pipelineLabel = getPipelineLabel(item.pipeline);
    const turnTypeLabel = getTurnTypeLabel(item.turn_type);
    const skillsCell = formatSkillsCell(item.skill_names);

    return `<tr data-trace-id="${escapeHtml(item.traceId)}">
      <td class="time-cell">${time}</td>
      <td><span class="trace-id">${escapeHtml(item.traceId)}</span></td>
      <td><span class="query-text" title="${query}">${query}</span></td>
      <td style="text-align:center"><span class="pipeline-badge pipeline-${escapeHtml(item.pipeline || "unknown")}">${escapeHtml(pipelineLabel)}</span></td>
      <td style="text-align:center"><span class="turntype-badge turntype-${escapeHtml(item.turn_type || "unknown")}">${escapeHtml(turnTypeLabel)}</span></td>
      <td><span class="skills-cell">${skillsCell}</span></td>
      <td style="text-align:center"><span class="status-badge ${statusClass}">${statusLabel}</span></td>
      <td style="text-align:center"><span class="mode-badge ${modeClass}">${modeLabel}</span></td>
      <td style="text-align:center"><span class="rating-badge ${ratingClass}">${ratingLabel}</span></td>
      <td style="text-align:right"><span class="tokens">${tokens}</span></td>
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
  if (status === "fallback" || status === "no_match" || status === "agent_fallback") return "status-fallback";
  return "status-unknown";
}

function getStatusLabel(status) {
  const map = {
    success: "成功",
    error: "错误",
    routing_error: "路由错误",
    fallback: "降级",
    agent_fallback: "Agent兜底",
    no_match: "无匹配",
    fallback_greeting: "问候",
    fallback_out_of_scope: "超范围",
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
    if (resp.status === 401) {
      window.location.href = "/admin/?redirect=/logs.html";
      return;
    }
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
  currentRating = "";
  const ratingFilter = document.getElementById("rating-filter");
  if (ratingFilter) ratingFilter.value = "";
  currentOffset = 0;
  fetchLogs();
});

// Rating filter
const ratingFilter = document.getElementById("rating-filter");
if (ratingFilter) {
  ratingFilter.addEventListener("change", () => {
    currentRating = ratingFilter.value;
    currentOffset = 0;
    fetchLogs();
  });
}

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

// === View Tab Switching ===
const viewTabs = document.querySelectorAll(".view-tab");
const statsPanel = document.getElementById("stats-panel");
const detailView = document.getElementById("detail-view");

viewTabs.forEach(tab => {
  tab.addEventListener("click", () => {
    viewTabs.forEach(t => t.classList.remove("active"));
    tab.classList.add("active");
    const view = tab.dataset.view;
    if (view === "stats") {
      statsPanel.classList.remove("hidden");
      detailView.classList.add("hidden");
      fetchStats();
    } else {
      statsPanel.classList.add("hidden");
      detailView.classList.remove("hidden");
    }
  });
});

// === Stats Panel ===
const statsDaysSelect = document.getElementById("stats-days");
const statsRefreshBtn = document.getElementById("stats-refresh-btn");

statsRefreshBtn.addEventListener("click", fetchStats);
statsDaysSelect.addEventListener("change", fetchStats);

async function fetchStats() {
  const days = statsDaysSelect.value;
  try {
    const resp = await fetch(`${API_BASE}/stats?days=${days}`);
    if (resp.status === 401) {
      window.location.href = "/admin/?redirect=/logs.html";
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderStats(data);
  } catch (err) {
    console.error("Stats fetch failed:", err);
  }
}

function renderStats(data) {
  // KPI Cards
  document.getElementById("kpi-pv").textContent = data.pv.toLocaleString();
  document.getElementById("kpi-uv").textContent = data.uv.toLocaleString();
  document.getElementById("kpi-rating-good").textContent = data.ratings.good.toLocaleString();
  document.getElementById("kpi-rating-ok").textContent = data.ratings.ok.toLocaleString();
  document.getElementById("kpi-rating-bad").textContent = data.ratings.bad.toLocaleString();

  // Daily Trend Chart (CSS bar chart)
  renderDailyChart(data.daily);
  // Ratings Chart
  renderRatingsChart(data.ratings);
  // Paths Chart
  renderBarChart("chart-paths", data.paths.map(p => ({ label: p.path, value: p.count })));
  // Modes Chart
  renderBarChart("chart-modes", data.modes.map(m => ({ label: m.mode, value: m.count })));

  // v0.5.1 BI 维度图
  const dims = data.dimensions || {};
  renderPipelineChart("chart-pipelines", dims.by_pipeline || []);
  renderBarChart(
    "chart-turn-types",
    (dims.by_turn_type || []).map(t => ({ label: getTurnTypeLabel(t.turn_type), value: t.count }))
  );
  renderBarChart(
    "chart-skills",
    (dims.by_skill || []).slice(0, 10).map(s => ({
      label: SKILL_DISPLAY_NAMES[s.skill_name] || s.skill_name,
      value: s.count,
    }))
  );
  renderBarChart(
    "chart-errors",
    (dims.by_error_reason || []).map(e => ({ label: getErrorReasonLabel(e.error_reason), value: e.count }))
  );
}

// Pipeline 专用图：在条形上叠加错误率百分比
function renderPipelineChart(containerId, items) {
  const container = document.getElementById(containerId);
  if (!items || items.length === 0) {
    container.innerHTML = '<div class="chart-empty">暂无数据</div>';
    return;
  }
  const maxVal = Math.max(...items.map(i => i.count), 1);
  const html = items.map(item => {
    const pct = Math.round((item.count / maxVal) * 100);
    const errPct = item.count > 0 ? Math.round((item.error_count / item.count) * 100) : 0;
    const avgLat = item.avg_latency_ms != null ? `${Math.round(item.avg_latency_ms)}ms` : "-";
    const errClass = errPct > 0 ? " has-error" : "";
    const label = getPipelineLabel(item.pipeline);
    return `<div class="h-bar-row${errClass}">
      <span class="h-bar-label" title="${escapeHtml(item.pipeline)}">${escapeHtml(label)}</span>
      <div class="h-bar-track">
        <div class="h-bar-fill" style="width:${pct}%"></div>
        ${errPct > 0 ? `<div class="h-bar-error-overlay" style="width:${pct * (errPct / 100)}%"></div>` : ""}
      </div>
      <span class="h-bar-value">${item.count} · 错误${errPct}% · ${avgLat}</span>
    </div>`;
  }).join("");
  container.innerHTML = html;
}

function renderDailyChart(daily) {
  const container = document.getElementById("chart-daily");
  if (!daily || daily.length === 0) {
    container.innerHTML = '<div class="chart-empty">暂无数据</div>';
    return;
  }
  const maxPv = Math.max(...daily.map(d => d.pv), 1);
  const bars = daily.map(d => {
    const height = Math.round((d.pv / maxPv) * 100);
    const dateLabel = d.date.slice(5); // MM-DD
    return `<div class="bar-col">
      <div class="bar-value">${d.pv}</div>
      <div class="bar" style="height:${height}%"></div>
      <div class="bar-label">${dateLabel}</div>
    </div>`;
  }).join("");
  container.innerHTML = `<div class="bar-chart">${bars}</div>`;
}

function renderRatingsChart(ratings) {
  const container = document.getElementById("chart-ratings");
  const total = ratings.bad + ratings.ok + ratings.good;
  if (total === 0) {
    container.innerHTML = '<div class="chart-empty">暂无评分数据</div>';
    return;
  }
  const items = [
    { label: "优秀", value: ratings.good, color: "#34d399" },
    { label: "一般", value: ratings.ok, color: "#d4a843" },
    { label: "糟糕", value: ratings.bad, color: "#f56565" },
  ];
  const html = items.map(item => {
    const pct = Math.round((item.value / total) * 100);
    return `<div class="h-bar-row">
      <span class="h-bar-label">${item.label}</span>
      <div class="h-bar-track">
        <div class="h-bar-fill" style="width:${pct}%;background:${item.color}"></div>
      </div>
      <span class="h-bar-value">${item.value} (${pct}%)</span>
    </div>`;
  }).join("");
  container.innerHTML = html;
}

function renderBarChart(containerId, items) {
  const container = document.getElementById(containerId);
  if (!items || items.length === 0) {
    container.innerHTML = '<div class="chart-empty">暂无数据</div>';
    return;
  }
  const maxVal = Math.max(...items.map(i => i.value), 1);
  const html = items.map(item => {
    const pct = Math.round((item.value / maxVal) * 100);
    return `<div class="h-bar-row">
      <span class="h-bar-label" title="${item.label}">${item.label.length > 15 ? item.label.slice(0, 15) + "…" : item.label}</span>
      <div class="h-bar-track">
        <div class="h-bar-fill" style="width:${pct}%"></div>
      </div>
      <span class="h-bar-value">${item.value}</span>
    </div>`;
  }).join("");
  container.innerHTML = html;
}

// === Init ===
fetchLogs();
