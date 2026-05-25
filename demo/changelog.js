/**
 * Laplace — Changelog Renderer
 * 从 changelog-data.json 加载版本数据并渲染到页面
 */

const SECTION_CONFIG = {
  features: { label: '功能更新', icon: '🚀', className: 'cl-section--features' },
  fixes: { label: '问题修复', icon: '🐛', className: 'cl-section--fixes' },
  others: { label: '其他', icon: '📋', className: 'cl-section--others' }
};

let changelogData = null;

async function loadChangelog() {
  try {
    const response = await fetch('changelog-data.json');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    changelogData = await response.json();
    renderVersionNav();
    selectVersionFromHash();
  } catch (error) {
    document.getElementById('version-content').innerHTML =
      '<div class="cl-loading">加载失败，请刷新重试</div>';
  }
}

function renderVersionNav() {
  const nav = document.getElementById('version-nav');
  const versions = changelogData.versions;

  nav.innerHTML = versions.map((version, index) =>
    `<button class="cl-version-btn${index === 0 ? ' active' : ''}"
             data-index="${index}"
             onclick="selectVersion(${index})">
      ${version.version}
    </button>`
  ).join('');
}

function selectVersion(index) {
  const versions = changelogData.versions;
  if (index < 0 || index >= versions.length) return;

  // Update nav active state
  document.querySelectorAll('.cl-version-btn').forEach((btn, i) => {
    btn.classList.toggle('active', i === index);
  });

  // Update URL hash
  const version = versions[index];
  history.replaceState(null, '', `#${version.version}`);

  // Render content
  renderVersionContent(version);
}

function selectVersionFromHash() {
  const hash = window.location.hash.slice(1);
  if (!hash || !changelogData) {
    selectVersion(0);
    return;
  }

  const index = changelogData.versions.findIndex(
    v => v.version === hash || v.version === `v${hash}`
  );
  selectVersion(index >= 0 ? index : 0);
}

function renderVersionContent(version) {
  const container = document.getElementById('version-content');

  const headerHtml = `
    <div class="cl-version-header">
      <div class="cl-version-meta">
        <span class="cl-version-badge">${version.version}</span>
        <span>${version.date}</span>
      </div>
      <h2 class="cl-version-title">${version.title}</h2>
    </div>
  `;

  // 兼容两种 JSON 结构：扁平（v0.3.7+）和嵌套 sections（v0.3.6 及之前）
  const getSection = (version, key) => version[key] || (version.sections && version.sections[key]);

  const sectionsHtml = Object.entries(SECTION_CONFIG)
    .filter(([key]) => {
      const items = getSection(version, key);
      return items && items.length > 0;
    })
    .map(([key, config]) => renderSection(key, config, getSection(version, key)))
    .join('');

  container.innerHTML = headerHtml + sectionsHtml;
}

function renderSection(key, config, items) {
  const itemsHtml = items.map(item => `
    <div class="cl-item">
      <div class="cl-item-title">${item.title}</div>
      <div class="cl-item-desc">${item.desc}</div>
    </div>
  `).join('');

  return `
    <div class="cl-section ${config.className}">
      <div class="cl-section-header">
        <span class="cl-section-icon">${config.icon}</span>
        ${config.label}
      </div>
      ${itemsHtml}
    </div>
  `;
}

// Listen for hash changes
window.addEventListener('hashchange', selectVersionFromHash);

// Initialize
loadChangelog();
