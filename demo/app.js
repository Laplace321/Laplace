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

// === Thinking Step Labels (两阶段 LLM 路由) ===
const THINKING_LABELS = {
  routing: "正在理解你的问题...",
  routed: "Skill 路由完成",
  filling_params: "正在解析查询参数...",
  querying: "正在检索从者数据...",
  generating: "正在生成分析...",
  // 兼容旧阶段名
  parsing: "正在理解你的问题...",
  parsed: "意图识别完成",
};

// === Preset Definitions (与后端 PRESET_REGISTRY 同步) ===
const PRESETS = {
  cycle_farming: {
    name: "cycle_farming",
    label: "周回筛选",
    icon: "⚡",
    fields: [
      { key: "search_by_np_charge", label: "NP 充能", type: "number", placeholder: "最低充能百分比（如 30）", default: 30, paramKey: "value", extraParams: { op: "gte" } },
      { key: "search_by_class", label: "职阶", type: "select", options: ["", "saber", "archer", "lancer", "rider", "caster", "assassin", "berserker", "ruler", "avenger", "alterEgo", "foreigner", "pretender", "moonCancer"], paramKey: "class_name" },
      { key: "search_by_rarity", label: "星级", type: "select", options: ["", "1", "2", "3", "4", "5"], paramKey: "value", extraParams: { op: "eq" } },
    ],
    response_skill: "respond_servant_list",
  },
  servant_lookup: {
    name: "servant_lookup",
    label: "从者查询",
    icon: "🔍",
    fields: [
      { key: "lookup_servant", label: "从者名称", type: "text", placeholder: "输入从者名称或昵称", paramKey: "name" },
    ],
    response_skill: "respond_servant_detail",
  },
  servant_compare: {
    name: "servant_compare",
    label: "从者对比",
    icon: "⚖️",
    fields: [
      { key: "compare_servants", label: "从者名称", type: "text", placeholder: "用逗号分隔多个名称（如：呆毛,小莫）", paramKey: "names", isArray: true },
    ],
    response_skill: "respond_servant_compare",
  },
  support_recommend: {
    name: "support_recommend",
    label: "辅助推荐",
    icon: "🛡️",
    fields: [
      { key: "search_by_skill_effect", label: "辅助类型", type: "select", options: ["gainNp", "upAtk", "upArts", "upBuster", "upQuick", "invincible", "avoidance"], paramKey: "effects", isArray: true, labels: { gainNp: "充能", upAtk: "攻击力提升", upArts: "Arts 提升", upBuster: "Buster 提升", upQuick: "Quick 提升", invincible: "无敌", avoidance: "回避" } },
    ],
    response_skill: "respond_support_analysis",
  },
};

// === Preset State ===
let activePreset = null;

// === Send Message (SSE Stream) ===
async function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || isProcessing) return;

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

  // If parsed, show conditions detail
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
        <div class="markdown-body">${replyHtml}</div>
        ${cardsHtml}
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

// === Preset Quick Query ===

function createPresetTabs() {
  const inputArea = document.getElementById("chat-input-area");
  const inputWrapper = inputArea.querySelector(".input-wrapper");

  // 创建标签容器
  const tabsContainer = document.createElement("div");
  tabsContainer.className = "preset-tabs";
  tabsContainer.id = "preset-tabs";

  for (const [key, preset] of Object.entries(PRESETS)) {
    const tab = document.createElement("button");
    tab.className = "preset-tab";
    tab.dataset.preset = key;
    tab.innerHTML = `<span class="preset-tab-icon">${preset.icon}</span><span>${preset.label}</span>`;
    tab.addEventListener("click", () => togglePreset(key));
    tabsContainer.appendChild(tab);
  }

  // 创建参数表单容器
  const formContainer = document.createElement("div");
  formContainer.className = "preset-form hidden";
  formContainer.id = "preset-form";

  // 插入到 input-wrapper 之前
  inputWrapper.parentNode.insertBefore(tabsContainer, inputWrapper);
  inputWrapper.parentNode.insertBefore(formContainer, inputWrapper);
}

function togglePreset(presetKey) {
  const form = document.getElementById("preset-form");
  const tabs = document.querySelectorAll(".preset-tab");

  if (activePreset === presetKey) {
    // 关闭当前预设
    activePreset = null;
    form.classList.add("hidden");
    form.innerHTML = "";
    tabs.forEach(t => t.classList.remove("active"));
    chatInput.placeholder = "输入你的问题... 例如「30 自充的从者有哪些」";
    return;
  }

  activePreset = presetKey;
  tabs.forEach(t => {
    t.classList.toggle("active", t.dataset.preset === presetKey);
  });

  renderPresetForm(PRESETS[presetKey]);
  form.classList.remove("hidden");
  chatInput.placeholder = "补充描述（可选）... 例如「只要五星的」";
}

function renderPresetForm(preset) {
  const form = document.getElementById("preset-form");
  const fieldsHtml = preset.fields.map(field => {
    if (field.type === "number") {
      return `
        <div class="preset-field">
          <label class="preset-label">${field.label}</label>
          <input type="number" class="preset-input" data-skill="${field.key}" data-param="${field.paramKey}"
                 placeholder="${field.placeholder || ''}" value="${field.default || ''}"
                 ${field.extraParams ? `data-extra='${JSON.stringify(field.extraParams)}'` : ""}>
        </div>
      `;
    } else if (field.type === "text") {
      return `
        <div class="preset-field">
          <label class="preset-label">${field.label}</label>
          <input type="text" class="preset-input" data-skill="${field.key}" data-param="${field.paramKey}"
                 placeholder="${field.placeholder || ''}"
                 ${field.isArray ? 'data-is-array="true"' : ""}>
        </div>
      `;
    } else if (field.type === "select") {
      const options = field.options.map(opt => {
        const label = field.labels ? (field.labels[opt] || opt) : (CLASS_NAMES[opt] || opt);
        return `<option value="${opt}">${opt === "" ? "全部" : label}</option>`;
      }).join("");
      return `
        <div class="preset-field">
          <label class="preset-label">${field.label}</label>
          <select class="preset-input" data-skill="${field.key}" data-param="${field.paramKey}"
                  ${field.isArray ? 'data-is-array="true"' : ""}
                  ${field.extraParams ? `data-extra='${JSON.stringify(field.extraParams)}'` : ""}>
            ${options}
          </select>
        </div>
      `;
    }
    return "";
  }).join("");

  form.innerHTML = `
    <div class="preset-form-header">
      <span class="preset-form-title">${preset.icon} ${preset.label}</span>
      <button class="preset-submit-btn" id="preset-submit-btn">查询</button>
    </div>
    <div class="preset-fields">${fieldsHtml}</div>
  `;

  document.getElementById("preset-submit-btn").addEventListener("click", () => sendPresetQuery(preset));
}

async function sendPresetQuery(preset) {
  if (isProcessing) return;

  // 收集表单参数
  const params = {};
  const inputs = document.querySelectorAll("#preset-form .preset-input");
  for (const input of inputs) {
    const skill = input.dataset.skill;
    const paramKey = input.dataset.param;
    const value = input.value.trim();
    if (!value) continue;

    if (!params[skill]) params[skill] = {};
    const extra = input.dataset.extra ? JSON.parse(input.dataset.extra) : {};
    Object.assign(params[skill], extra);

    if (input.dataset.isArray === "true") {
      params[skill][paramKey] = value.includes(",")
        ? value.split(",").map(s => s.trim()).filter(Boolean)
        : [value];
    } else if (input.type === "number") {
      params[skill][paramKey] = parseInt(value, 10);
    } else {
      params[skill][paramKey] = value;
    }
  }

  // 检查至少一个参数
  if (Object.keys(params).length === 0) {
    showToast("请至少填写一个查询条件");
    return;
  }

  const supplement = chatInput.value.trim() || null;
  const displayText = `[${preset.label}] ${Object.entries(params).map(([k, v]) => JSON.stringify(v)).join(" + ")}${supplement ? ` + "${supplement}"` : ""}`;

  isProcessing = true;
  sendBtn.disabled = true;
  sendBtn.classList.add("loading");
  chatInput.value = "";

  appendMessage("user", displayText);
  chatHistory.push({ role: "user", text: displayText });
  saveSession();

  // 使用 POST /api/chat（非 SSE），因为 preset 模式不走 stream
  const typingEl = appendTypingIndicator();

  try {
    const resp = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: "preset",
        preset_name: preset.name,
        params,
        supplement,
        response_skill: preset.response_skill,
      }),
    });

    typingEl.remove();

    if (!resp.ok) throw new Error(`服务器错误 (${resp.status})`);
    const data = await resp.json();

    appendAssistantResponse(data);

    if (data.model) modelName.textContent = data.model;
    if (data.traceId) {
      lastTraceId = data.traceId;
      updateDebugPanel();
    }

    chatHistory.push({
      role: "assistant",
      html: chatMessages.lastElementChild.querySelector(".message-bubble")?.innerHTML || "",
    });
    saveSession();
  } catch (err) {
    typingEl.remove();
    showToast(`请求失败: ${err.message}`);
  } finally {
    isProcessing = false;
    sendBtn.disabled = false;
    sendBtn.classList.remove("loading");
    chatInput.focus();
  }
}

// === Event Listeners ===
sendBtn.addEventListener("click", sendMessage);

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// Suggestion chips
document.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) {
    chatInput.value = e.target.dataset.query;
    sendMessage();
  }
});

// Focus input on load + init preset tabs
document.addEventListener("DOMContentLoaded", () => {
  chatInput.focus();
  createDebugPanel();
  createPresetTabs();
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

// Init history panel on load
document.addEventListener("DOMContentLoaded", () => {
  createHistoryPanel();
});
