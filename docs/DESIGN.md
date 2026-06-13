# Laplace · DESIGN.md

> Laplace 前端视觉系统规范。本文件是产品所有前端页面的视觉真理来源（Source of Truth），任何 UI 变更需先回到此文件确认 token 与组件契约，再落地代码。

- **版本**：v3.0 · 「迦勒底档案室 Chaldea Archive」
- **范围**：`demo/` 主聊天前端、`demo/changelog.html` 更新日志、`demo/logs.html` Trace 查询、`admin/` 管理后台
- **不在范围**：`docs/*.html`（项目介绍页）、`demo/prototypes/`（原型对照）、第三方 `chaldea-center/`

---

## 1. Visual Theme · 视觉主题

### 设计立意
**「迦勒底中央档案室的一份解密公文」** —— 玩家不是在用一个 AI Chatbot，而是在 Chaldea 中央情报室翻阅一份关于英灵的机密档案。每次提问被处理为「一次召唤指令的归档」，每次回答被呈现为「一份盖章的检索回执」。

### 三个核心隐喻
| 隐喻 | 表现 |
|:-----|:-----|
| **档案纸张** | 纸白底色 + 浅蓝坐标网格 + 微微的纸张噪点感（通过双向 `repeating-linear-gradient` 实现） |
| **公文加印** | 关键操作按钮使用 `clip-path` 切角（如信封折角），重要标题带罗马数字徽章，效果标签像盖章 chip |
| **海军蓝制式** | 主色为 FGO 官网海军蓝 `#0A1F4A`，次色为古金 `#C9A961`，避免任何霓虹/紫色/纯黑 |

### 关键词
**严肃 / 可信 / 复古制式 / 实体感** —— 不要"未来感"、不要"赛博"、不要"AI 极简"。

---

## 2. Color Palette · 色板

### 2.1 核心 Token（必须使用 CSS 变量，禁止硬编码）

```css
:root {
  /* === 底层 === */
  --bg:        #F5F6F8;  /* 纸白底色：所有页面 body 背景 */
  --paper:     #FFFFFF;  /* 卡片/气泡/弹窗：纯白 */
  --paper-2:   #FAFBFD;  /* 二级卡片/输入框背景：略带蓝调 */
  --grid:      #E6EDF7;  /* 网格底纹颜色（极淡蓝）*/

  /* === 主色（海军蓝）=== */
  --ink-deep:  #061535;  /* 标题/重要文字 */
  --ink:       #0A1F4A;  /* 主色：按钮填充、Logo、链接 */
  --ink-soft:  #4A5878;  /* 次级文字、标签 */
  --ink-mute:  #8893AB;  /* 占位符、辅助说明 */

  /* === 点缀（金色）=== */
  --gold-deep: #8E7234;  /* 暗金：徽章 hover */
  --gold:      #C9A961;  /* 主金：徽章、强调点 */
  --gold-soft: #D4B987;  /* 浅金：金色卡片底色 */
  --gold-bg:   rgba(201,169,97,0.10); /* 金色背景填充 */

  /* === 辅助蓝（Chips / Hover / 高亮）=== */
  --cyan:        #2E7BC4;  /* 信息蓝：chip 文字、链接 hover */
  --cyan-soft:   #EAF1FB;  /* chip 底色 */
  --cyan-border: rgba(46,123,196,0.30);

  /* === 状态色（克制使用）=== */
  --ok:    #2D8659;  /* 成功 */
  --warn:  #B8801F;  /* 警告（用古金而非纯橙）*/
  --err:   #B23B3B;  /* 错误（朱红，对齐文人印章感）*/

  /* === 分隔线 === */
  --rule:     #D8DEE8;  /* 主分隔线（虚线/实线）*/
  --rule-soft:#ECEEF3;  /* 弱分隔线 */
}
```

### 2.2 稀有度色板（从者卡片专用）
```css
--rarity-5: #C9A961;  /* ★5 古金 */
--rarity-4: #B89968;  /* ★4 暗金 */
--rarity-3: #8893AB;  /* ★3 银 */
--rarity-2: #B89968;  /* ★2 铜 */
--rarity-1: #B89968;
--rarity-0: #8893AB;
```

> ★5 不再用 FGO 游戏内的明亮黄（`#F5C842`），改为古金 `#C9A961`，以贴合公文档案的克制感。

### 2.3 禁用色
**严禁出现以下颜色**（这些是旧深色版本残留，会立刻破坏档案室氛围）：
- `#0a0b1a` / `#12142b` / `#191c3a` 任何深紫蓝背景
- `#8b5cf6` / `#a78bfa` 紫色
- `#22d3ee` / `#00d4ff` 霓虹青
- 任何 `rgba(255,255,255,0.x)` 半透明白底（在白底上没意义）

---

## 3. Typography · 字体系统

### 3.1 字体堆栈

```css
--font-display: 'Cinzel', 'Cormorant Garamond', 'Noto Serif SC', serif;
--font-serif:   'Cormorant Garamond', 'EB Garamond', 'Noto Serif SC', serif;
--font-body:    'Noto Sans SC', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif;
--font-mono:    'IBM Plex Mono', 'JetBrains Mono', 'SF Mono', Menlo, monospace;
```

### 3.2 用途分配
| 场景 | 字体 | Size / Weight / Letter-spacing |
|:-----|:-----|:-------------------------------|
| Logo `LAPLACE` | Cinzel 700 | 20px / 700 / 6px |
| Tagline `AI NATIVE FGO ASSISTANT` | Cinzel 600 | 9px / 600 / 3px / uppercase |
| 段落正文（AI 回复 / 用户气泡） | Noto Sans SC | 14px / 400 / 0 |
| 从者名（中文） | Noto Serif SC 700 | 17px / 700 / 0.5px |
| 从者名（英文别名） | Cinzel 600 | 10px / 600 / 2px / uppercase |
| 章节标题（H2/H3） | Cormorant Garamond 700 | 28px / 700 / 1px / italic |
| 数字/Trace ID/版本号 | IBM Plex Mono | 12px / 500 |
| Chip 标签（`50 自充`） | Noto Sans SC 600 | 11px / 600 / 0.5px |
| 思考步骤 | Noto Sans SC | 12px / 400 |

### 3.3 字体引入
所有页面通过国内镜像引入：
```html
<link rel="preconnect" href="https://fonts.loli.net" crossorigin>
<link rel="preconnect" href="https://gstatic.loli.net" crossorigin>
<link href="https://fonts.loli.net/css2?family=Cinzel:wght@400;600;700&family=Cormorant+Garamond:ital,wght@0,500;0,700;1,500&family=Noto+Sans+SC:wght@400;500;600;700&family=Noto+Serif+SC:wght@500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
```

> 禁止直接使用 `fonts.googleapis.com` 等被墙域名（参考 AGENTS.md 第 12 条）。

---

## 4. Components · 组件规范

### 4.1 按钮 Buttons

#### 主按钮（提交 / 保存）
- 背景：`var(--ink)` 海军蓝填充
- 文字：`var(--gold)` 金色 / Cinzel 600 / letter-spacing 2px
- 边框：无
- 形状：`clip-path: polygon(8px 0, 100% 0, calc(100% - 8px) 100%, 0 100%)` 双向折角
- 圆角：**0**（不要任何 `border-radius`）
- Hover：`background: var(--ink-deep)`，金字略微发光 `text-shadow: 0 0 8px rgba(201,169,97,0.4)`
- Active：`transform: translate(1px, 1px)` 模拟按下

#### 次按钮（取消 / 返回）
- 背景：`var(--paper)` 白
- 文字：`var(--ink)` 海军蓝
- 边框：`1px solid var(--ink)`
- 形状：同上 clip-path
- Hover：`background: var(--ink); color: var(--gold);` 反色

#### 危险按钮（删除 / 重启）
- 背景：`var(--paper)` 白
- 文字：`var(--err)` 朱红
- 边框：`1px solid var(--err)`
- Hover：`background: var(--err); color: var(--paper);` 反色

#### 图标按钮（Header 上的 +/历史/清空）
- 背景：transparent
- 边框：`1px solid var(--rule)`
- 圆角：**0**
- 颜色：`var(--ink-soft)`
- Hover：`color: var(--ink); border-color: var(--ink);`

### 4.2 卡片 Cards · 从者卡（核心组件）

> 这是产品内最高频的卡片类型，定义参见 `demo/prototypes/a-archive.html`。

**结构**：横排紧凑列表式（非纸牌网格）
```
┌──────────────────────────────────────────┐
│ ▏[face]  从者中文名 · ENGLISH NAME        │
│ ▏       ★★★★★  ·  CASTER               │
│ ▏─────────────────────────────────────   │
│ ▏ [50 自充] [Arts 宝具]                  │
└──────────────────────────────────────────┘
   ↑ 3px 蓝色色条（hover 变金）
```

**CSS 契约**：
```css
.chat-card {
  background: var(--paper);
  border: 1px solid var(--rule);
  border-radius: 0;                                /* 直角！*/
  padding: 12px 14px;
  display: flex; flex-direction: column; gap: 10px;
  position: relative;
  transition: all 0.2s;
}
.chat-card::before {
  content:""; position:absolute;
  top:0; left:0; bottom:0; width:3px;
  background: var(--ink);
}
.chat-card:hover {
  border-color: var(--ink);
  box-shadow: 4px 4px 0 var(--ink-deep);          /* 偏移阴影 */
  transform: translate(-2px, -2px);
}
.chat-card:hover::before { background: var(--gold); }
```

**Grid 布局**：`grid-template-columns: repeat(auto-fit, minmax(340px, 1fr))`（聊天区 2 列）

**face 头像**：`56×56`，`1.5px solid var(--ink)` 边框，**直角**（不是圆角）

### 4.3 Chip 标签

> 用于显示从者效果（如「50 自充」「Arts 宝具」「30 他充+10 群充」），所有 chip 必须是中文（参考 AGENTS.md 第 9 条）。

```css
.chip {
  display: inline-flex; align-items: center;
  font-family: var(--font-body);
  font-size: 11px; font-weight: 600;
  letter-spacing: 0.5px;
  padding: 3px 9px;
  background: var(--cyan-soft);
  color: var(--cyan);
  border: 1px solid var(--cyan-border);
  clip-path: polygon(4px 0, 100% 0, calc(100% - 4px) 100%, 0 100%);
}
.chip.gold {
  background: var(--gold-bg);
  color: var(--gold-deep);
  border-color: rgba(201,169,97,0.4);
}
```

### 4.4 气泡 Bubbles

#### 用户气泡
- 背景：`var(--ink)` 海军蓝
- 文字：`var(--paper)` 白
- 形状：`clip-path: polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 0 100%)` 右上折角
- 圆角：**0**

#### AI 气泡
- 背景：`var(--paper)` 白
- 文字：`var(--ink-deep)` 深蓝
- 边框：`1px solid var(--rule)`
- 左侧装饰：`4px solid var(--ink)` 竖条
- 圆角：**0**

### 4.5 输入框

```css
#chat-input {
  background: var(--paper-2);
  border: 1px solid var(--ink);                   /* 注意是主色边框，强调输入态 */
  border-radius: 0;
  padding: 12px 18px;
  font-family: var(--font-body);
  color: var(--ink-deep);
}
#chat-input:focus { border-color: var(--gold); }
#chat-input::placeholder { color: var(--ink-mute); }
```

输入框上方装饰条（仅主聊天 input area）：
```css
#chat-input-area::before {
  content:""; position:absolute; top:0; left:0; right:0; height:2px;
  background: linear-gradient(90deg,
    var(--ink) 0%, var(--ink) 30%,
    var(--gold) 30%, var(--gold) 35%,
    var(--ink) 35%, var(--ink) 100%);
}
```

### 4.6 弹窗 Modal

- 遮罩：`background: rgba(6, 21, 53, 0.55)` 深海军蓝半透
- 卡身：`var(--paper)` 白底，`1px solid var(--ink)` 蓝边
- 阴影：`box-shadow: 8px 8px 0 var(--ink-deep)` 偏移硬阴影（非 blur）
- 顶部装饰条：与输入框同款蓝金条纹
- 关闭按钮：右上角 `×`，`var(--ink-soft)` → hover `var(--err)`

### 4.7 历史侧栏 / Tabs / 表格

- 表格表头：`var(--paper-2)` 浅蓝白底 + `var(--ink)` 文字 + `2px solid var(--ink)` 下边框
- 表格行：`1px solid var(--rule-soft)` 分隔；hover `background: var(--cyan-soft)`
- Tab：未选中 `color: var(--ink-soft)` + `border-bottom: 2px solid transparent`；选中 `color: var(--ink)` + `border-bottom: 2px solid var(--gold)` + `font-weight: 700`

---

## 5. Layout · 布局

### 5.1 全局栅格
- **主聊天区最大宽度**：`max-width: 880px`（原 800，扩到 880 以容纳横排卡片）
- **管理后台最大宽度**：`max-width: 1180px`
- **侧边栏宽度**：280px（历史抽屉）
- **页面 padding**：`var(--space-lg) = 24px`（桌面）/ `var(--space-md) = 16px`（移动）

### 5.2 Spacing Token
```css
--space-xs: 4px;  --space-sm: 8px;
--space-md: 16px; --space-lg: 24px;
--space-xl: 32px; --space-2xl: 48px;
```

### 5.3 网格底纹（body 背景）
```css
body {
  background: var(--bg);
  background-image:
    repeating-linear-gradient(0deg,   transparent 0, transparent 23px, var(--grid) 23px, var(--grid) 24px),
    repeating-linear-gradient(90deg,  transparent 0, transparent 23px, var(--grid) 23px, var(--grid) 24px);
}
```

> 仅 `body` 加底纹，所有内部容器（卡片、气泡）保持纯白，避免视觉混乱。

---

## 6. Depth & Effects · 层级与效果

### 6.1 阴影哲学：**偏移硬阴影**，禁止柔性 blur

档案室视觉的核心是"实体感"，所以：
- 卡片 hover：`box-shadow: 4px 4px 0 var(--ink-deep)`（无 blur）
- 弹窗：`box-shadow: 8px 8px 0 var(--ink-deep)`
- 按钮 hover：仅 `transform`，不加阴影
- **禁止**：`box-shadow: 0 4px 24px rgba(0,0,0,0.4)` 这类柔性阴影

### 6.2 圆角哲学：**全部直角**

```css
--radius-0: 0;     /* 默认所有元素 */
```

唯一例外：从者头像的内部 face 图（避免方形 face 视觉过硬，保留 0px 直角，与卡片整体一致）。

### 6.3 切角 Clip-path

仅在以下场景使用：
- 主按钮 / 次按钮（双向折角）
- Chip 标签（轻微折角，4px）
- 用户气泡（右上折角，14px，模拟信封）

**禁止**：导航栏、卡片整体、输入框使用 clip-path（会破坏对齐）。

### 6.4 动画

- 进场：`opacity 0.3s + translateY(8px → 0)`，禁用 `scale`
- Hover：`transition: all 0.2s`，禁用 cubic-bezier 复杂曲线
- 思考步骤 active：金色脉冲 `text-shadow` 呼吸（保留现有 `pulse-glow` 关键帧）
- **禁止**：弹跳、霓虹流光、彩虹渐变

---

## 7. Do's and Don'ts · 应做 / 不应做

### ✅ Do
1. 所有颜色使用 `var(--xxx)` 变量
2. 所有圆角默认为 0；如需软化用 1-2px
3. 卡片优先使用「白底 + 1px 蓝边 + 偏移硬阴影」
4. 中文用 Noto Sans SC，英文标题用 Cinzel
5. 重要按钮使用切角 clip-path 体现公文感
6. 任何效果数值（如 NP 充能）使用 chip 形式展示，不要用图标 + 数字
7. 阴影必须是无 blur 的偏移硬阴影
8. 状态色（OK/WARN/ERR）保持克制，主要靠文字表达，不要用大色块

### ❌ Don't
1. 禁止任何深色背景（用户前端 100% 浅色档案室）
2. 禁止霓虹色、紫色、彩虹渐变
3. 禁止柔性 blur 阴影
4. 禁止圆角大于 4px 的元素（除头像内部图片外）
5. 禁止用 emoji 作为视觉主元素（emoji 仅在 chip 标签内或 button label 内辅助使用）
6. 禁止暴露技术术语（如 `skill_name`、`subStatePositive`），所有用户可见文本必须中文化（AGENTS.md 第 9 条）
7. 禁止在卡片上使用阴影 + 渐变 + 圆角同时三件套（这是 AI 风的标志）
8. 禁止字体使用 `Orbitron`（旧版残留科技感字体，已从体系中移除）

---

## 8. Responsive · 响应式

### Breakpoints
| 断点 | 名称 | 主要变化 |
|:-----|:-----|:---------|
| `>768px` | desktop | 卡片网格 2 列、Header 显示完整 model-badge |
| `≤768px` | tablet | 卡片网格压缩到 1 列、隐藏 model-badge、缩小 Logo 字号到 18px |
| `≤480px` | mobile | 隐藏 tagline、Logo 字号 16px、Footer disclaimer 字号到 9px |
| `≤360px` | narrow | 隐藏 disclaimer、Header 紧凑模式 |

### 关键规则
- 桌面卡片 grid：`repeat(auto-fit, minmax(340px, 1fr))`
- 移动卡片 grid：`grid-template-columns: 1fr`（强制 1 列）
- 移动 padding：`16px`；桌面 `24px`
- 切角 clip-path 在 `≤480px` 时减半（如 `8px → 4px`），避免视觉过强

---

## 9. Agent Prompt Guide · 给 Agent 的 Prompt 引导

> 当你（AI Agent）被要求**修改 / 新增** Laplace 前端页面时，必须遵循下方 prompt 模板和 checklist。

### 9.1 必读约束
- 用户前端的所有视觉变更必须优先回看本 `DESIGN.md`
- 严禁未经讨论引入新的颜色 token / 字体 / 圆角值
- 任何卡片/气泡/按钮新增前，先在第 4 节查找是否已有规范
- 所有用户可见文本必须中文化（参考 AGENTS.md 第 9 条「前端零技术术语」）

### 9.2 新增组件 Checklist
新增任何 UI 组件时，逐条确认：
```
□ 颜色全部使用 :root 中的 CSS 变量，未硬编码 hex
□ 圆角值为 0（除头像内部 face 外）
□ 字体使用 var(--font-display/--font-serif/--font-body/--font-mono) 之一
□ 间距使用 var(--space-xx) 而非裸数字（除非 < 4px 微调）
□ 阴影是偏移硬阴影 (offset 0 var(--ink-deep))，不带 blur
□ 没有引入紫/霓虹青/彩虹渐变
□ 没有暴露 skill_name/subStatePositive 等技术术语
□ 移动端断点（≤768px / ≤480px）已考虑
```

### 9.3 改动 demo/style.css 后必须做
按 AGENTS.md 第 12 条「前端静态资源部署纪律」：
1. 同步递增 `demo/index.html` 中 `style.css?v=N` 版本号
2. 同步递增 `demo/changelog.html` / `demo/logs.html` / `admin/index.html` 中各自 CSS 的 `?v=N`
3. `demo/index.html` 提交前先 `git checkout origin/main -- demo/index.html` 防止本地字体污染
4. 不允许 `docker exec sed` 直接改容器，必须 build → run 完整流程

### 9.4 配色变量备忘（最高频）
```
背景：     --bg #F5F6F8     纸白
卡身：     --paper #FFFFFF  白
主色：     --ink #0A1F4A    海军蓝
次色：     --gold #C9A961   古金
标题文字： --ink-deep #061535
正文文字： --ink-deep
次级文字： --ink-soft #4A5878
分隔线：   --rule #D8DEE8
Chip 蓝：  --cyan #2E7BC4 + --cyan-soft #EAF1FB
```

---

## 附录 A · 改造前后对照

| 维度 | 旧版（深色 AI 风） | 新版（FGO 档案室）|
|:-----|:------------------|:-----------------|
| 主背景 | `#0a0b1a` 深紫蓝 | `#F5F6F8` 纸白 + 浅蓝网格 |
| 主色 | `#d4a843` 暖金 + `#8b5cf6` 紫 | `#0A1F4A` 海军蓝 + `#C9A961` 古金 |
| 字体 | Orbitron + Noto Sans SC | Cinzel + Cormorant Garamond + Noto Sans/Serif SC + IBM Plex Mono |
| 圆角 | 6/10/16/20px | **全部 0** |
| 阴影 | `0 4px 24px rgba(0,0,0,0.4)` 柔性 blur | `4px 4px 0 var(--ink-deep)` 偏移硬阴影 |
| 按钮 | 圆形渐变金 | 切角 clip-path + 蓝填金字 |
| 卡片 | 深底 + 圆角 + 柔性阴影 | 白底 + 直角 + 左侧蓝色色条 + 偏移硬阴影 |
| 用户气泡 | 圆角 + 紫色 | 直角 + 海军蓝 + 右上信封折角 |

---

**修订记录**

| 日期 | 版本 | 变更 |
|:-----|:-----|:-----|
| 2026-06-10 | v3.0 | 初版：从 5 套原型评估中确定方案 A「迦勒底档案室」，全量替换旧深色 AI 风 |
