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

// === Thinking Step Labels ===
const THINKING_LABELS = {
  parsing: "正在理解你的问题...",
  parsed: "意图识别完成",
  querying: "正在检索从者数据...",
  generating: "正在生成分析...",
};

// === Send Message (SSE Stream) ===
async function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || isProcessing) return;

  isProcessing = true;
  sendBtn.disabled = true;
  chatInput.value = "";

  appendMessage("user", text);

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
    appendMessage("assistant", `⚠️ 请求失败: ${err.message}\n\n请确保后端服务已启动 (uvicorn server.main:app)`, true);
  } finally {
    isProcessing = false;
    sendBtn.disabled = false;
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
      handleDone(data);
      break;
    case "error":
      handleError(data, els);
      break;
  }
  scrollToBottom();
}

// === Handle Thinking Events ===
function handleThinking(data, els) {
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
function handleDone(data) {
  if (data.model && data.model !== "error") {
    modelName.textContent = data.model;
  }
  if (data.traceId) {
    lastTraceId = data.traceId;
    updateDebugPanel();
  }
}

// === Handle Error Event ===
function handleError(data, els) {
  const activeStep = els.thinkingSteps.querySelector(".thinking-step.active");
  if (activeStep) {
    activeStep.classList.remove("active");
    activeStep.classList.add("error");
    const icon = activeStep.querySelector(".thinking-step-icon");
    if (icon) icon.textContent = "✗";
  }
  els.replyBody.style.display = "";
  els.replyBody.innerHTML = `<p>⚠️ ${escapeHtml(data.message || "请求失败")}</p>`;
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

  // 获取最大自充值显示
  let chargeDisplay = "";
  if (servant.npCharges && servant.npCharges.length > 0) {
    const charges = servant.npCharges.map(c => `${c.chargePercent}%`).join("+");
    chargeDisplay = charges;
  } else if (servant.maxSelfCharge) {
    chargeDisplay = `${servant.maxSelfCharge}%`;
  }

  return `
    <div class="chat-card rarity-${servant.rarity}" style="animation-delay: ${Math.min(index * 20, 400)}ms">
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

// Suggestion chips
document.addEventListener("click", (e) => {
  if (e.target.classList.contains("chip")) {
    chatInput.value = e.target.dataset.query;
    sendMessage();
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
