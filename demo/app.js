/**
 * Laplace — Chat App Logic v3
 *
 * 对话式交互：发送消息 → SSE 流式接收 → 分阶段渲染 Thinking Steps + 卡片 + 文字
 * 向后兼容：保留旧 JSON 端点常量供调试使用
 */

const API_URL = "http://localhost:8000/api/chat";
const STREAM_API_URL = "/api/chat/stream";

// === Class Display Names ===
const CLASS_NAMES = {
  saber: "Saber", archer: "Archer", lancer: "Lancer",
  rider: "Rider", caster: "Caster", assassin: "Assassin",
  berserker: "Berserker", ruler: "Ruler", avenger: "Avenger",
  moonCancer: "Moon Cancer", alterEgo: "Alter Ego",
  foreigner: "Foreigner", pretender: "Pretender",
  shielder: "Shielder", beast: "Beast",
};

// === DOM ===
const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const modelName = document.getElementById("model-name");
const chatContainer = document.getElementById("chat-container");

// === State ===
let isProcessing = false;
let lastTraceId = null;
let debugVisible = false;

// === Chat History (localStorage) ===
const STORAGE_KEY = "laplace_chat_history";
let currentSessionId = Date.now().toString(36);
let chatHistory = []; // 当前会话的消息数组

// === Preset Definitions (mirror server/skills/presets.py) ===
const PRESETS = [
  {
    name: "cycle_farming",
    label: "周回筛选",
    description: "按充能、职阶、稀有度筛选周回从者",
    defaultMessage: "帮我找适合周回的从者",
  },
  {
    name: "servant_lookup",
    label: "从者查询",
    description: "查询单个从者的详细信息",
    defaultMessage: "查一下梅林的详细信息",
  },
  {
    name: "servant_compare",
    label: "从者对比",
    description: "对比多个从者的能力",
    defaultMessage: "对比村正和武尊",
  },
  {
    name: "support_recommend",
    label: "辅助推荐",
    description: "筛选辅助向从者并推荐搭配",
    defaultMessage: "有充能技能的辅助从者",
  },
];

// === Thinking Step Labels ===
const THINKING_LABELS = {
  // Skill 路由阶段（新）
  routing: "正在理解你的问题...",
  routed: "意图识别完成",
  executing: "正在检索从者数据...",
  generating: "正在生成分析...",
  // 旧阶段名兼容映射
  parsing: "正在理解你的问题...",
  parsed: "意图识别完成",
  querying: "正在检索从者数据...",
};

// === Skill Display Names (internal name → user-friendly Chinese) ===
const SKILL_DISPLAY_NAMES = {
  search_by_effect: "效果筛选",
  search_by_np_charge: "宝具充能筛选",
  search_by_class: "职阶筛选",
  search_by_rarity: "稀有度筛选",
  search_by_attribute: "属性筛选",
  search_by_skill_effect: "技能效果筛选",
  search_by_np_effect: "宝具效果筛选",
  search_by_traits: "特性筛选",
  search_by_cards: "配卡筛选",
  lookup_servant: "从者查询",
  compare_servants: "从者对比",
};

function getSkillDisplayName(skillName) {
  return SKILL_DISPLAY_NAMES[skillName] || skillName;
}

// === Send Preset Query (via SSE /api/chat/stream with preset_name) ===
// Tag Pill mode: userText is the natural language supplement typed after the pill.
// If empty, uses preset defaults only. Backend handles B1 supplement parsing.
// Uses the same SSE streaming pipeline as manual input for consistent thinking steps UX.
async function sendPresetQuery(presetName, userText) {
  if (isProcessing) return;

  isProcessing = true;
  sendBtn.disabled = true;
  sendBtn.classList.add("loading");
  chatInput.value = ""; // ensure input is cleared

  const preset = PRESETS.find(p => p.name === presetName);
  const presetLabel = preset ? preset.label : presetName;
  // Display message: show preset label + user text
  const displayMsg = userText ? `[${presetLabel}] ${userText}` : `[${presetLabel}]`;
  appendMessage("user", displayMsg);
  chatHistory.push({ role: "user", text: displayMsg });
  saveSession();

  // Use streaming container (same as manual input) for consistent thinking steps
  const els = createStreamingContainer();

  try {
    const effectiveMessage = userText || (preset ? preset.defaultMessage : "") || "";
    const url = `${STREAM_API_URL}?message=${encodeURIComponent(effectiveMessage)}&preset_name=${encodeURIComponent(presetName)}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`服务器错误 (${resp.status})`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const events = parseSSE(buffer);
      buffer = events.remainder;

      for (const ev of events.parsed) {
        handleStreamEvent(ev.event, ev.data, els);
      }
    }

    // Finalize: remove cursor if present
    const cursor = els.replyBody.querySelector(".stream-cursor");
    if (cursor) cursor.remove();

  } catch (err) {
    els.container.remove();
    showToast(`请求失败: ${err.message}`);
  } finally {
    isProcessing = false;
    sendBtn.disabled = false;
    sendBtn.classList.remove("loading");
    chatInput.focus();
  }
}

// === Preset Bar: Render persistent tabs above input ===
let activePreset = null;

function renderPresetBar() {
  const bar = document.getElementById("preset-bar");
  if (!bar) return;
  bar.innerHTML = PRESETS.map(p =>
    `<button class="preset-bar-tab" data-preset="${p.name}" title="${escapeHtml(p.description)}">${escapeHtml(p.label)}</button>`
  ).join("");
}

// === Tag Pill: Activate/Deactivate Preset ===
function activatePreset(presetName) {
  const preset = PRESETS.find(p => p.name === presetName);
  if (!preset) return;

  activePreset = presetName;

  // Show pill in input wrapper
  const pill = document.getElementById("preset-pill");
  if (pill) {
    pill.innerHTML = `<span class="preset-pill-icon">⧫</span><span class="preset-pill-label">${escapeHtml(preset.label)}</span><button class="preset-pill-close" title="取消">&times;</button>`;
    pill.classList.remove("hidden");
  }

  // Highlight active tab
  document.querySelectorAll(".preset-bar-tab").forEach(tab => {
    tab.classList.toggle("active", tab.dataset.preset === presetName);
  });

  // Update placeholder
  chatInput.placeholder = preset.description + "...";
  chatInput.focus();
}

function deactivatePreset() {
  activePreset = null;

  const pill = document.getElementById("preset-pill");
  if (pill) {
    pill.classList.add("hidden");
    pill.innerHTML = "";
  }

  // Remove active state from tabs
  document.querySelectorAll(".preset-bar-tab").forEach(tab => tab.classList.remove("active"));

  // Restore placeholder
  chatInput.placeholder = "输入你的问题... 例如「30 自充的从者有哪些」";
}

// === Send Message (SSE Stream or Preset) ===
async function sendMessage() {
  const text = chatInput.value.trim();
  if (isProcessing) return;

  // If preset is active, route to preset mode
  if (activePreset) {
    const presetName = activePreset;
    const userText = text; // may be empty — uses preset defaults
    chatInput.value = "";
    deactivatePreset();
    sendPresetQuery(presetName, userText);
    return;
  }

  if (!text) return;

  isProcessing = true;
  sendBtn.disabled = true;
  sendBtn.classList.add("loading");
  chatInput.value = "";

  appendMessage("user", text);
  chatHistory.push({ role: "user", text });
  saveSession();

  // Create streaming container (replaces typing indicator)
  const els = createStreamingContainer();

  try {
    const url = `${STREAM_API_URL}?message=${encodeURIComponent(text)}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`服务器错误 (${resp.status})`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const events = parseSSE(buffer);
      buffer = events.remainder;

      for (const ev of events.parsed) {
        handleStreamEvent(ev.event, ev.data, els);
      }
    }

    // Finalize: remove cursor if present
    const cursor = els.replyBody.querySelector(".stream-cursor");
    if (cursor) cursor.remove();

  } catch (err) {
    els.container.remove();
    showToast(`请求失败: ${err.message}`);
  } finally {
    isProcessing = false;
    sendBtn.disabled = false;
    sendBtn.classList.remove("loading");
    chatInput.focus();
  }
}

// === Parse SSE Text into Events ===
function parseSSE(text) {
  const parsed = [];
  const lines = text.split("\n");
  let currentEvent = null;
  let currentData = null;
  let lastProcessedIndex = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.startsWith("event: ")) {
      currentEvent = line.slice(7).trim();
    } else if (line.startsWith("data: ")) {
      currentData = line.slice(6);
    } else if (line === "" && currentEvent && currentData !== null) {
      try {
        parsed.push({ event: currentEvent, data: JSON.parse(currentData) });
      } catch (e) {
        console.warn("SSE parse error:", currentData);
      }
      currentEvent = null;
      currentData = null;
      lastProcessedIndex = i + 1;
    }
  }

  // Keep unprocessed remainder for next chunk
  const remainderLines = lines.slice(lastProcessedIndex);
  const remainder = remainderLines.join("\n");

  return { parsed, remainder };
}

// === Create Streaming Container ===
function createStreamingContainer() {
  const msg = document.createElement("div");
  msg.className = "message assistant-message";

  const thinkingSteps = document.createElement("div");
  thinkingSteps.className = "thinking-steps";

  const cardsArea = document.createElement("div");
  cardsArea.className = "chat-cards-grid stream-hidden";

  const replyBody = document.createElement("div");
  replyBody.className = "markdown-body stream-hidden";

  msg.innerHTML = `
    <div class="message-avatar">⧫</div>
    <div class="message-content">
      <div class="message-bubble"></div>
    </div>
  `;

  const bubble = msg.querySelector(".message-bubble");

  // 骨架屏占位 — 收到第一个 thinking 事件后移除
  const skeleton = document.createElement("div");
  skeleton.className = "skeleton-placeholder";
  skeleton.innerHTML = `
    <div class="skeleton-line w80"></div>
    <div class="skeleton-line w60"></div>
    <div class="skeleton-line w40"></div>
  `;
  bubble.appendChild(skeleton);

  bubble.appendChild(thinkingSteps);
  bubble.appendChild(cardsArea);
  bubble.appendChild(replyBody);

  chatMessages.appendChild(msg);
  scrollToBottom();

  return { container: msg, thinkingSteps, cardsArea, replyBody };
}

// === Handle Stream Event ===
function handleStreamEvent(eventType, data, els) {
  switch (eventType) {
    case "thinking":
      handleThinking(data, els);
      break;
    case "servants":
      handleServants(data, els);
      break;
    case "delta":
      handleDelta(data, els);
      break;
    case "done":
      handleDone(data, els);
      break;
    case "error":
      handleError(data, els);
      break;
  }
  scrollToBottom();
}

// === Handle Thinking Events ===
function handleThinking(data, els) {
  // 移除骨架屏（首次 thinking 事件时）
  const skel = els.container.querySelector(".skeleton-placeholder");
  if (skel) skel.remove();

  // Complete previous active step
  const activeStep = els.thinkingSteps.querySelector(".thinking-step.active");
  if (activeStep) completeThinkingStep(activeStep);

  const label = data.message || THINKING_LABELS[data.phase] || data.phase;
  const step = renderThinkingStep(data.phase, label, els.thinkingSteps);

  // If routed (Skill mode), show pre-digested Chinese detail from backend
  if (data.phase === "routed") {
    completeThinkingStep(step);
    if (data.detail) {
      const detail = document.createElement("div");
      detail.className = "thinking-step-detail";
      detail.textContent = data.detail;
      els.thinkingSteps.appendChild(detail);
    }
  }

  // If parsed (legacy mode), show conditions detail
  if (data.phase === "parsed" && data.conditions) {
    completeThinkingStep(step);
    const keys = Object.keys(data.conditions);
    if (keys.length > 0) {
      const detail = document.createElement("div");
      detail.className = "thinking-step-detail";
      detail.textContent = JSON.stringify(data.conditions);
      els.thinkingSteps.appendChild(detail);
    }
  }
}

// === Handle Servants Event ===
function handleServants(data, els) {
  // Complete querying step
  const activeStep = els.thinkingSteps.querySelector(".thinking-step.active");
  if (activeStep) completeThinkingStep(activeStep);

  if (data.servants && data.servants.length > 0) {
    els.cardsArea.innerHTML = data.servants
      .map((s, i) => createCardHtml(s, i))
      .join("");
    // 触发 reflow 后移除 hidden，实现 opacity 渐入
    void els.cardsArea.offsetHeight;
    els.cardsArea.classList.remove("stream-hidden");
  }
}

// === Handle Delta Event ===
function handleDelta(data, els) {
  // Complete generating step
  const activeStep = els.thinkingSteps.querySelector(".thinking-step.active");
  if (activeStep) completeThinkingStep(activeStep);

  const replyHtml = typeof marked !== "undefined"
    ? marked.parse(data.text)
    : `<p>${escapeHtml(data.text)}</p>`;
  els.replyBody.innerHTML = replyHtml + '<span class="stream-cursor"></span>';
  void els.replyBody.offsetHeight;
  els.replyBody.classList.remove("stream-hidden");
}

// === Handle Done Event ===
function handleDone(data, els) {
  if (data.model && data.model !== "error") {
    modelName.textContent = data.model;
  }
  if (data.traceId) {
    lastTraceId = data.traceId;
    updateDebugPanel();
  }
  // 追踪助手回复到历史（保存完整 HTML 快照以保留样式）
  if (els) {
    const bubbleEl = els.container.querySelector(".message-bubble");
    const html = bubbleEl ? bubbleEl.innerHTML : "";
    chatHistory.push({ role: "assistant", html });
    saveSession();
  }
}

// === Handle Error Event ===
function handleError(data, els) {
  // 移除骨架屏
  const skel = els.container.querySelector(".skeleton-placeholder");
  if (skel) skel.remove();

  const activeStep = els.thinkingSteps.querySelector(".thinking-step.active");
  if (activeStep) {
    activeStep.classList.remove("active");
    activeStep.classList.add("error");
    const icon = activeStep.querySelector(".thinking-step-icon");
    if (icon) icon.textContent = "✗";
  }
  els.replyBody.style.display = "";
  els.replyBody.innerHTML = `<p>⚠️ ${escapeHtml(data.message || "请求失败")}</p>`;
  showToast(data.message || "请求失败");
}

// === Render a Thinking Step ===
function renderThinkingStep(phase, message, container) {
  const step = document.createElement("div");
  step.className = "thinking-step active";
  step.dataset.phase = phase;
  step.innerHTML = `
    <span class="thinking-step-icon">◆</span>
    <span class="thinking-step-text">${escapeHtml(message)}</span>
  `;
  container.appendChild(step);
  return step;
}

// === Complete a Thinking Step ===
function completeThinkingStep(stepEl) {
  stepEl.classList.remove("active");
  stepEl.classList.add("completed");
  const icon = stepEl.querySelector(".thinking-step-icon");
  if (icon) icon.textContent = "✓";
}

// === Append User/Assistant Message ===
function appendMessage(role, text, isError = false) {
  const msg = document.createElement("div");
  msg.className = `message ${role}-message`;

  const avatar = role === "user" ? "👤" : "⧫";

  msg.innerHTML = `
    <div class="message-avatar">${avatar}</div>
    <div class="message-content">
      <div class="message-bubble ${isError ? 'error-bubble' : ''}">
        <p>${escapeHtml(text)}</p>
      </div>
    </div>
  `;

  chatMessages.appendChild(msg);
  scrollToBottom();
}

// === Append Assistant Response with Cards ===
function appendAssistantResponse(data) {
  const msg = document.createElement("div");
  msg.className = "message assistant-message";

  // Thinking steps: show pre-digested Chinese filter descriptions
  let thinkingHtml = "";
  const appliedFilters = data.query?.applied_filters || [];
  if (appliedFilters.length > 0) {
    const filterDetail = appliedFilters.join("、");
    thinkingHtml = `
      <div class="thinking-steps">
        <div class="thinking-step completed">
          <span class="thinking-step-icon">✓</span>
          <span class="thinking-step-text">意图识别完成</span>
        </div>
        <div class="thinking-step-detail">${escapeHtml(filterDetail)}</div>
        <div class="thinking-step completed">
          <span class="thinking-step-icon">✓</span>
          <span class="thinking-step-text">检索到 ${data.count || 0} 位从者</span>
        </div>
      </div>`;
  }

  let cardsHtml = "";
  if (data.servants && data.servants.length > 0) {
    cardsHtml = `<div class="chat-cards-grid">
      ${data.servants.map((s, i) => createCardHtml(s, i)).join("")}
    </div>`;
  }

  // 如果引入了 marked，则解析 Markdown，否则兜底安全转义
  const replyHtml = typeof marked !== 'undefined' ? marked.parse(data.reply) : `<p>${escapeHtml(data.reply)}</p>`;

  msg.innerHTML = `
    <div class="message-avatar">⧫</div>
    <div class="message-content">
      <div class="message-bubble">
        ${thinkingHtml}
        ${cardsHtml}
        <div class="markdown-body">${replyHtml}</div>
      </div>
    </div>
  `;

  chatMessages.appendChild(msg);
  scrollToBottom();
}

// === Create Card HTML ===
function createCardHtml(servant, index) {
  const stars = getStars(servant.rarity);
  const className = CLASS_NAMES[servant.className] || servant.className;

  // 获取充能展示（三分类：自充/他充/群充）
  let chargeDisplay = "";
  if (servant.npCharges && servant.npCharges.length > 0) {
    const charges = servant.npCharges.map(c => {
      const label = c.targetType === 'self' ? '自充'
        : c.targetType === 'ptOne' ? '他充' : '群充';
      return `${c.chargePercent}${label}`;
    }).join('+');
    chargeDisplay = charges;
  } else if (servant.maxSelfCharge) {
    chargeDisplay = `自充${servant.maxSelfCharge}%`;
  }

  return `
    <div class="chat-card rarity-${servant.rarity}" style="animation-delay: ${Math.min(index * 20, 400)}ms">
      <div class="chat-card-row">
        <div class="chat-card-face">
          <img src="${servant.faceUrl}" alt="${servant.name}" loading="lazy"
               onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 42 42%22><rect fill=%22%23191c3a%22 width=%2242%22 height=%2242%22/><text x=%2221%22 y=%2225%22 text-anchor=%22middle%22 fill=%22%235c5a6e%22 font-size=%2212%22>?</text></svg>'">
          <div class="chat-card-face-border"></div>
        </div>
        <div class="chat-card-info">
          <div class="chat-card-name" title="${servant.aliasCN || servant.name}">${servant.aliasCN || servant.name}</div>
          <div class="chat-card-meta">
            <span class="chat-card-stars">${stars}</span>
            <span>${className}</span>
          </div>
        </div>
      </div>
      ${chargeDisplay ? `<div class="chat-card-charge">${chargeDisplay}</div>` : ""}
    </div>
  `;
}

// === Typing Indicator ===
function appendTypingIndicator() {
  const msg = document.createElement("div");
  msg.className = "message assistant-message";
  msg.innerHTML = `
    <div class="message-avatar">⧫</div>
    <div class="message-content">
      <div class="message-bubble">
        <div class="typing-indicator">
          <div class="typing-dot"></div>
          <div class="typing-dot"></div>
          <div class="typing-dot"></div>
        </div>
      </div>
    </div>
  `;
  chatMessages.appendChild(msg);
  scrollToBottom();
  return msg;
}

// === Toast ===
function showToast(message, type = "error") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("visible"));
  setTimeout(() => {
    toast.classList.remove("visible");
    toast.addEventListener("transitionend", () => toast.remove());
  }, 3000);
}

// === Helpers ===
function getStars(rarity) {
  if (rarity === 0) return "☆";
  return "★".repeat(rarity);
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    chatContainer.scrollTop = chatContainer.scrollHeight;
  });
}

// === Event Listeners ===
sendBtn.addEventListener("click", sendMessage);

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// Suggestion chips + Preset bar + Pill close (event delegation)
document.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) {
    chatInput.value = e.target.dataset.query;
    sendMessage();
  }
  // Preset bar tab toggle → show/hide tag pill
  if (e.target.classList.contains("preset-bar-tab")) {
    const presetName = e.target.dataset.preset;
    if (activePreset === presetName) {
      deactivatePreset();
    } else {
      activatePreset(presetName);
    }
  }
  // Pill close button
  if (e.target.classList.contains("preset-pill-close")) {
    deactivatePreset();
  }
});

// Focus input on load
document.addEventListener("DOMContentLoaded", () => {
  chatInput.focus();
  createDebugPanel();
});

// === Debug Panel (Ctrl+D, localhost only) ===
function createDebugPanel() {
  const panel = document.createElement("div");
  panel.id = "debug-panel";
  panel.className = "debug-panel hidden";
  panel.innerHTML = `
    <span class="debug-label">DEBUG</span>
    <span class="debug-trace">
      trace: <span id="debug-trace-id">-</span>
    </span>
    <button id="debug-copy-btn" class="debug-copy" title="复制 trace_id">复制</button>
  `;
  document.body.appendChild(panel);

  document.getElementById("debug-copy-btn").addEventListener("click", () => {
    if (!lastTraceId) return;
    navigator.clipboard.writeText(lastTraceId).then(() => {
      const btn = document.getElementById("debug-copy-btn");
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = "复制"; }, 1500);
    });
  });
}

function toggleDebugPanel() {
  const panel = document.getElementById("debug-panel");
  if (!panel) return;
  if (debugVisible) {
    panel.classList.remove("hidden");
  } else {
    panel.classList.add("hidden");
  }
}

function updateDebugPanel() {
  const el = document.getElementById("debug-trace-id");
  if (el && lastTraceId) {
    el.textContent = lastTraceId;
  }
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "d") {
    e.preventDefault();
    debugVisible = !debugVisible;
    toggleDebugPanel();
  }
});

// === Session Persistence ===
function saveSession() {
  const sessions = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  const idx = sessions.findIndex(s => s.id === currentSessionId);
  const sessionData = {
    id: currentSessionId,
    timestamp: Date.now(),
    messages: chatHistory,
    preview: chatHistory.find(m => m.role === "user")?.text?.slice(0, 40) || "新会话",
  };
  if (idx >= 0) sessions[idx] = sessionData;
  else sessions.push(sessionData);
  // 最多保留 20 个会话
  while (sessions.length > 20) sessions.shift();
  localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
}

function loadSessions() {
  return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]").sort((a, b) => b.timestamp - a.timestamp);
}

function deleteSession(sessionId) {
  const sessions = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  const filtered = sessions.filter(s => s.id !== sessionId);
  localStorage.setItem(STORAGE_KEY, JSON.stringify(filtered));
}

// === History Panel ===
function createHistoryPanel() {
  // Overlay
  const overlay = document.createElement("div");
  overlay.className = "history-overlay";
  overlay.id = "history-overlay";
  overlay.addEventListener("click", closeHistoryPanel);
  document.body.appendChild(overlay);

  // Panel
  const panel = document.createElement("div");
  panel.className = "history-panel";
  panel.id = "history-panel";
  panel.innerHTML = `
    <div class="history-panel-header">
      <h3>历史会话</h3>
      <div class="history-header-actions">
        <button class="history-clear-all-btn" id="history-clear-all-btn" title="清空全部历史">清空</button>
        <button class="history-close-btn" id="history-close-btn">&times;</button>
      </div>
    </div>
    <div class="history-list" id="history-list"></div>
  `;
  document.body.appendChild(panel);

  document.getElementById("history-close-btn").addEventListener("click", closeHistoryPanel);
  document.getElementById("history-clear-all-btn").addEventListener("click", () => {
    if (!confirm("确定清空全部历史会话？此操作不可恢复。")) return;
    localStorage.removeItem(STORAGE_KEY);
    loadSessionList();
    showToast("已清空全部历史", "error");
  });
}

function openHistoryPanel() {
  loadSessionList();
  document.getElementById("history-overlay").classList.add("visible");
  document.getElementById("history-panel").classList.add("visible");
}

function closeHistoryPanel() {
  document.getElementById("history-overlay").classList.remove("visible");
  document.getElementById("history-panel").classList.remove("visible");
}

function loadSessionList() {
  const list = document.getElementById("history-list");
  const sessions = loadSessions();
  if (sessions.length === 0) {
    list.innerHTML = '<div class="history-empty">暂无历史会话</div>';
    return;
  }
  list.innerHTML = sessions.map(s => {
    const time = new Date(s.timestamp).toLocaleString("zh-CN", {
      month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"
    });
    const msgCount = s.messages ? s.messages.length : 0;
    return `
      <div class="history-item" data-session-id="${s.id}">
        <div class="history-item-body">
          <div class="history-item-preview">${escapeHtml(s.preview)}</div>
          <div class="history-item-time">${time} · ${msgCount} 条消息</div>
        </div>
        <button class="history-item-delete" data-delete-id="${s.id}" title="删除此会话">&times;</button>
      </div>
    `;
  }).join("");

  // 点击会话恢复
  list.querySelectorAll(".history-item-body").forEach(body => {
    body.addEventListener("click", () => {
      const sessionId = body.closest(".history-item").dataset.sessionId;
      restoreSession(sessionId);
      closeHistoryPanel();
    });
  });

  // 单条删除
  list.querySelectorAll(".history-item-delete").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const deleteId = btn.dataset.deleteId;
      deleteSession(deleteId);
      loadSessionList(); // 刷新列表
    });
  });
}

function restoreSession(sessionId) {
  const sessions = loadSessions();
  const session = sessions.find(s => s.id === sessionId);
  if (!session || !session.messages) return;

  // 切换到该会话
  currentSessionId = sessionId;
  chatHistory = [...session.messages];

  // 清空聊天区域并重绘（保留完整样式）
  chatMessages.innerHTML = "";
  for (const msg of session.messages) {
    if (msg.role === "assistant" && msg.html) {
      // 助手消息：使用保存的 HTML 快照恢复完整样式
      const div = document.createElement("div");
      div.className = "message assistant-message";
      div.innerHTML = `
        <div class="message-avatar">⧫</div>
        <div class="message-content">
          <div class="message-bubble">${msg.html}</div>
        </div>
      `;
      chatMessages.appendChild(div);
    } else {
      appendMessage(msg.role, msg.text);
    }
  }
  scrollToBottom();
}

// === Welcome Message ===
function appendWelcomeMessage() {
  const msg = document.createElement("div");
  msg.className = "message assistant-message";
  msg.innerHTML = `
    <div class="message-avatar">⧫</div>
    <div class="message-content">
      <div class="message-bubble">
        <p>你好，Master！我是 <strong>Laplace</strong>，你的 FGO 数据助手。</p>
        <p>你可以用自然语言向我提问，例如：</p>
        <div class="suggestion-chips">
          <button class="chip" data-query="帮我找一下 30 自充的从者有哪些">30 自充的从者</button>
          <button class="chip" data-query="大于 50 自充的从者有哪些">50% 以上自充</button>
          <button class="chip" data-query="五星 Caster 有自充的从者">五星 Caster 自充</button>
          <button class="chip" data-query="四星以上的 Berserker 有哪些">四星以上狂阶</button>
        </div>
      </div>
    </div>
  `;
  chatMessages.appendChild(msg);
}

// === Clear + History Button Events ===
document.getElementById("clear-btn").addEventListener("click", () => {
  if (!confirm("确定清空当前对话？")) return;
  chatMessages.innerHTML = "";
  chatHistory = [];
  currentSessionId = Date.now().toString(36);
  appendWelcomeMessage();
});

document.getElementById("history-btn").addEventListener("click", () => {
  openHistoryPanel();
});

// Init on load
document.addEventListener("DOMContentLoaded", () => {
  createHistoryPanel();
  renderPresetBar();
  // Inject welcome message dynamically
  if (chatMessages.querySelectorAll(".message").length === 0) {
    appendWelcomeMessage();
  }
});
