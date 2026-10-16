# PgVN 监控页面视觉重设计方案

日期：2026 年 10 月 15 日。状态：已实现并完成本机回归；Windows 高对比度人工视觉验收待补。

范围：`web/pgvn-monitor/` 下的 `index.html`、`monitor.css`、`monitor.js` 三个内嵌静态资源。后端 API、路由、CSP、令牌与采样逻辑都不变。相关文档：[M4 监控说明](../pgvn/M4-monitor.zh-CN.md)、[开发推进计划](2026-10-15-pgvn-implementation-plan.zh-CN.md)。

设计依据：`ui-ux-pro-max` 技能检索结果（设计系统、图表、样式、配色、UX、图标几类领域，查询记录见附录 A）。所有配色已按 WCAG 2.2 算过对比度，结果见 4.3 节。

---

## 0 摘要

当前页面信息齐全，可访问性基础也不错，但视觉上还是“表格 + 卡片”的通用后台：状态信号分散在三处，最关键的数据新鲜度放在页脚，KPI 只有数字没有趋势，图表是单色折线，图标靠 Unicode 字符拼凑，也没有暗色模式。

新方案取名 **“夜航控制台（Night Ops）”**：

1. **双主题**：暗色作为主设计，亮色同等适配，默认跟随系统，也可手动切换。
2. **一条健康条（Health Strip）**：把“服务就绪 / 数据库采样 / 写主 / 节点在线 / 样本新鲜度”收在页面顶部同一位置，看一眼就知道整体状态。
3. **KPI 卡片带趋势**：每张卡片配迷你趋势线或容量条，并标出与窗口均值的差异。
4. **新的图表**：面积渐变、末值标注、虚线网格，可用键盘读取数值；不同指标用不同线型区分，不只靠颜色。
5. **完整的设计令牌和 SVG 图标精灵**：取代散落的硬编码颜色和 Unicode 图标。
6. **完全遵守现有约束**：不加载外部字体或 CDN，不使用内联样式，不新增路由，不改数据契约。

---

## 1 现状分析

### 1.1 结构

| 区域 | 实现 | 位置 |
| --- | --- | --- |
| 左侧导航 | 222px 固定深色栏 `#111d31`，品牌块 + 5 个导航项 + 底部说明 | `index.html` L12–23 |
| 顶栏 | 74px 白底，面包屑 + 刷新周期下拉 + 暂停/刷新/断开按钮 | `index.html` L25–27 |
| 页头 | eyebrow + h1 + 副标题，右侧就绪徽章 | `index.html` L29 |
| 连接状态条 | `role=status` 的 live 区域，左边框色条 | `index.html` L30 |
| 登录面板 | 两栏卡片：说明 + 令牌表单 | `index.html` L31–34 |
| 内容区 | 由 `render()` 动态生成：`.stats` 四卡 → `.columns` 图表/详情 → 分页表格 | `monitor.js` L139–186 |
| 页脚 | 产品名 + 采样时间 `#sample-meta` | `index.html` L37 |

### 1.2 现有样式令牌

`monitor.css` 第 1 行只定义了 14 个变量：`--canvas --surface --ink --muted --line --primary --nav --good(-bg) --warn(-bg) --bad(-bg) --radius`。另外至少有 20 处硬编码颜色（`#edf2fb`、`#aebed5`、`#1c4bbb`、`#638feb`、`#e8edf5`、`#fafbfd`、`#f8faff`、`#1e2d46`、`#284774`、`#83abff`、`#ead49e`、`#fff8e8` 等），全部只有亮色值，`color-scheme:light`。

### 1.3 值得保留的优点

- **安全**：所有数据都用 `textContent` 和 `createElement` 写入，fixture 里的恶意仓库名 `<img src=x onerror=…>` 只会显示成文本。新方案必须保持这一点。
- **可访问性基础**：有跳过链接、`sr-only`、`focus-visible` 外框、单一 `aria-live` 状态区、`prefers-reduced-motion`、图表附带数据表 `<details>`，刷新后还会恢复焦点和展开状态（`render()` L141–185）。
- **文案**：中文文案准确，“未知”语义严格（数据过期时远端数值显示“未知”，不沿用旧值）。
- **刷新节流**：页面隐藏时降频到 ≥30 秒，支持暂停，用 epoch 防止竞态。

### 1.4 问题清单

| # | 类别 | 问题 | 证据 | 影响 |
| --- | --- | --- | --- | --- |
| P1 | 图标 | 导航图标用 `◈ ◇ ▤ ▥ ≋` 等 Unicode 字符 | `index.html` L16–20 | 显示效果依赖字体，Windows Server 默认字体下粗细和基线不一致；技能明确列为反模式 |
| P2 | 状态层级 | 就绪徽章、连接状态条、数据库过期 `.alert` 分散在三处 | `index.html` L29–30，`monitor.js` L147 | 运维人员需要扫视多处才能判断整体健康状况 |
| P3 | 新鲜度 | “采样于 … · N 秒前”放在页脚 | `index.html` L37 | 监控页最重要的“数据有多新”反而最不显眼 |
| P4 | KPI | 四张卡片外观相同，只有数字，没有趋势、阈值和状态色 | `stat()` L20，L150 | 无法判断数字是好是坏、在升还是在降 |
| P5 | 图表 | 单色 2.5px 折线，没有面积、末值、悬停或焦点读数；三张图样式完全相同；`.area` 类已定义但从未使用 | `chart()` L81–95 | 不同指标难区分，想看具体值只能展开表格 |
| P6 | 字号 | 有 10px 文字（`.stat-note`、`.eyebrow`、`.page-footer`、`td .badge`、`.chart text`），大量 11px | CSS | 低于密集仪表盘 12px 的下限，高 DPI 缩放下更难读 |
| P7 | 主题 | 没有暗色模式；令牌不完整，主题无法切换 | CSS L1 | 机房、NOC 大屏和夜间值班场景下刺眼 |
| P8 | 表格 | 表头不固定；数字列左对齐，没有 `tabular-nums`；ID 列截断后没有完整值提示 | `table()` L24 | 长列表滚动后看不到列名，数字不对齐，难以比较 |
| P9 | 加载 | 切换视图时只显示“正在读取状态”空态文字，刷新时只把按钮设为半透明 | `navigate()` L195 | 布局跳动，等待期间没有明确的进度提示 |
| P10 | 动效 | 完全没有过渡，悬停时背景色瞬间切换 | CSS | 界面显得生硬 |
| P11 | 响应式 | ≤760px 时导航变成换行的按钮堆，图标缩到 15px | CSS `@media(max-width:760px)` | 窄屏导航占两三行，挤压内容 |
| P12 | Windows 高对比度 | 没有 `forced-colors` 适配，徽章只靠背景色区分 | CSS | Windows 高对比度主题下徽章边界消失 |
| P13 | 可维护性 | CSS 压缩成 5 行 | `monitor.css` | 难以审阅和比较差异，与 256 KiB 的资源上限无关 |

---

## 2 设计约束（硬性）

| 约束 | 来源 | 对设计的要求 |
| --- | --- | --- |
| 只有 3 个资源文件，每个 ≤256 KiB，不含 NUL | `scripts/pgvn/embed_web.py` L11–14 | 图标精灵内联在 `index.html`；不能新增文件 |
| 只有 `/monitor/`、`/monitor/monitor.css`、`/monitor/monitor.js` 三个静态路由 | `src/pgvn/monitor/http_win.c` L108–111 | 不能加 favicon、字体或图片文件 |
| CSP：`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'` | `http_win.c` L29 | **不能加载 Web 字体**（`font-src` 会回落到 `none`）；不能用 `<style>`、`style=""` 或 `data:` 图片；JS 不得拼接 `style` 属性 |
| 运行不依赖 Node 或 CDN | M4 文档 | 不能用 Chart.js、GSAP、Tailwind 构建；图表继续手写 SVG，动效用 CSS |
| 令牌不能持久保存 | M4 浏览器检查 `no_persistent_token` | 主题偏好**不写入** `localStorage` 或 `sessionStorage`（见 9.3） |
| API 契约不变 | `data.c` `pgvn_monitor_api` | 只使用现有的 overview、nodes、repos、repos/{id}、transactions、requests、events、trends、series |
| 资源 SHA-256 已写入证据 | `docs/pgvn/evidence/m4*.json`、`monitor_browser_fixture.py` L42 | 修改后必须按 M4 文档重跑浏览器验收并更新证据 |
| 现有 DOM ID 被 JS 和测试依赖 | `monitor.js` | 保留下列 ID：`login token-form token token-error content search-form repository-search clear-search refresh pause logout refresh-interval readiness connection-status page-title subtitle breadcrumb sample-meta main` |

---

## 3 设计方向

### 3.1 技能推荐结果（`--design-system`，dials：variance 4 / motion 3 / density 8）

| 维度 | 技能结果 | 采纳 | 说明 |
| --- | --- | --- | --- |
| Pattern | Real-Time / Operations | ✅ | 只有当前有数据时才标“实时”，同时显示更新时间、过期状态、暂停和刷新频率控制 |
| Style | Dark Mode (OLED) + Data-Dense Dashboard | ✅ | 暗色为主；12 栅格、紧凑卡片、固定表头 |
| 主色 | `#16A34A` 绿 | ❌ 改为蓝 `#5B9BFF` | 绿色专门表示“健康”。主色也用绿会让“按钮”和“正常”难以区分 |
| 强调色 | `#DC2626` 红 | ❌ 只用于错误状态 | 红色只表示故障，不用于普通操作按钮 |
| 字体 | Fira Code + Fira Sans | ⚠️ 换成系统字体 | CSP 和离线要求不允许加载外部字体，改用同类系统字体（见 4.4） |
| 动效 | GSAP Scroll Reveal | ❌ 改用 CSS 过渡 | 不能引入 GSAP；监控页也不需要滚动入场动画 |
| 反模式 | 加载慢的仪表盘、装饰性图表、隐藏错误状态 | ✅ 全部遵守 | 每张图都对应一个可读的数值；错误一律放在显眼位置 |

### 3.2 设计原则

1. **状态优先**：先回答“现在正常吗？数据有多新？”，再展示细节。
2. **一种颜色只表达一种含义**：蓝色表示可交互或品牌，绿、琥珀、红、灰分别表示正常、警告、故障、未知，不作他用。
3. **不能只靠颜色**：每个状态都同时有图标、文字和颜色；图表系列再加线型区分。
4. **数字要好读**：数值统一用等宽数字，单位缩小显示，“未知”显示为灰色 `—` 并附带“未知”文字。
5. **克制的动效**：只在状态变化和用户操作时出现，减少动效模式下完全静止。

---

## 4 设计令牌

### 4.1 颜色：暗色（主设计）

```css
:root[data-theme="dark"] {
  color-scheme: dark;
  /* 表面层级：越高越亮 */
  --bg:            #0B1120;  /* 页面底 */
  --bg-grid:       #0E1526;  /* 背景点阵/分区 */
  --surface-1:     #111A2E;  /* 卡片、面板 */
  --surface-2:     #17223A;  /* 表头、悬停、输入框 */
  --surface-3:     #1E2A45;  /* 选中、弹出层 */
  --nav:           #080D1A;  /* 侧栏 */
  --border:        #2A3854;  /* 分隔线（装饰性） */
  --border-strong: #607294;  /* 控件边界：相对 surface-2 为 3.266:1 */
  /* 文字 */
  --text:          #E6EDF7;
  --text-2:        #A3B1C6;  /* 次要说明 */
  --text-3:        #8593AB;  /* 第三级：时间戳、单位 */
  /* 品牌与交互 */
  --primary:       #5B9BFF;
  --primary-hover: #7AB0FF;
  --on-primary:    #0B1120;
  --primary-soft:  rgb(91 155 255 / .14);
  --focus:         #8DBBFF;
  /* 语义状态：前景 / 底色 */
  --good:    #34D399;  --good-bg:    rgb(52 211 153 / .14);
  --warn:    #FBBF24;  --warn-bg:    rgb(251 191 36 / .14);
  --bad:     #F87171;  --bad-bg:     rgb(248 113 113 / .14);
  --info:    #60A5FA;  --info-bg:    rgb(96 165 250 / .14);
  --neutral: #94A3B8;  --neutral-bg: rgb(148 163 184 / .14);
  /* 数据系列 */
  --series-1: #5B9BFF;  /* 命令速率 · 实线 */
  --series-2: #FB7185;  /* 失败率 · 虚线 6 4 */
  --series-3: #A78BFA;  /* 延迟 · 点线 2 4 */
  --series-4: #22D3EE;  /* CPU / 资源 */
  --chart-grid: #1E2A45;
  --shadow: 0 1px 0 rgb(255 255 255 / .03) inset, 0 8px 24px rgb(0 0 0 / .35);
}
```

### 4.2 颜色：亮色

```css
:root[data-theme="light"] {
  color-scheme: light;
  --bg: #F4F6FA; --bg-grid: #EEF1F6;
  --surface-1: #FFFFFF; --surface-2: #F8FAFD; --surface-3: #EEF3FB;
  --nav: #0F1A2E;                       /* 侧栏在亮色下仍保持深色，作为品牌锚点 */
  --border: #DFE5EE; --border-strong: #8492A8;
  --text: #0F1A2E; --text-2: #4A5870; --text-3: #5F6E86;
  --primary: #2457D6; --primary-hover: #1C47B4; --on-primary: #FFFFFF;
  --primary-soft: rgb(36 87 214 / .08); --focus: #2457D6;
  --good: #0F7A44;    --good-bg: #E7F2EC;
  --warn: #8A5A00;    --warn-bg: #F3EEE6;
  --bad: #B42335;     --bad-bg: #F8E9EB;
  --info: #1D5FC9;    --info-bg: #E8EFFA;
  --neutral: #526178; --neutral-bg: #EEEFF2;
  --series-1: #2457D6; --series-2: #C0264B; --series-3: #6D4ED8; --series-4: #0E7490;
  --chart-grid: #E6EBF2;
  --shadow: 0 1px 2px rgb(15 26 46 / .06), 0 4px 16px rgb(15 26 46 / .06);
}
```

主题选择逻辑（CSS 部分）：没有 `data-theme` 属性时用 `@media (prefers-color-scheme: dark)` 套用暗色令牌；JS 只在用户手动切换时设置 `data-theme`。

### 4.3 对比度核验（WCAG 2.2，已计算）

以下为最终 CSS 令牌重新计算的实际组合。透明徽章底色与卡片底合成；完整 38 项见本次证据。该计算不等同于 DevTools Accessibility 面板或 Windows 高对比度视觉验收。

| 组合 | 暗色 | 亮色 | 要求 |
| --- | --- | --- | --- |
| 正文 / 页面底 | 15.979 | 16.074 | ≥4.5 |
| 次要文字 / 卡片 | 7.978 | 7.189 | ≥4.5 |
| 三级文字 / 输入框底 | 5.096 | 4.945 | ≥4.5 |
| 主按钮文字 / 主色 | 6.794 | 6.160 | ≥4.5 |
| 侧栏文字 / 侧栏 | 16.458 | 14.759 | ≥4.5 |
| 侧栏次要文字 / 侧栏 | 8.923 | 8.001 | ≥4.5 |
| 侧栏就绪 / 侧栏 | 10.088 | 9.046 | ≥4.5 |
| 控件边界 / 输入框底 | 3.266 | 3.016 | ≥3 |
| 焦点外框 / 卡片 | 8.823 | 6.160 | ≥3 |
| 系列线 1 / 卡片 | 6.257 | 6.160 | ≥3 |
| 系列线 2 / 卡片 | 6.443 | 5.801 | ≥3 |
| 系列线 3 / 卡片 | 6.372 | 5.628 | ≥3 |
| 系列线 4 / 卡片 | 9.595 | 5.358 | ≥3 |
| 徽章 good / 合成底色 | 6.867 | 4.710 | ≥4.5 |
| 徽章 warn / 合成底色 | 7.800 | 5.132 | ≥4.5 |
| 徽章 bad / 合成底色 | 5.172 | 5.521 | ≥4.5 |
| 徽章 info / 合成底色 | 5.390 | 5.140 | ≥4.5 |
| 徽章 neutral / 合成底色 | 5.361 | 5.469 | ≥4.5 |

### 4.4 字体

CSP 不允许加载字体，所以把技能推荐的 “Fira Code + Fira Sans（数据用等宽，标签用无衬线）” 换成 Windows 自带的同类字体：

```css
--font-sans: "Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
--font-mono: "Cascadia Mono", "Cascadia Code", Consolas, "Microsoft YaHei Mono", ui-monospace, monospace;
```

| 令牌 | 字号 / 行高 / 字重 | 用途 |
| --- | --- | --- |
| `--fs-xs` | 12 / 16 / 500 | 表头、eyebrow（大写字母间距 .08em）、徽章、坐标轴（**最小字号，不再使用 10–11px**） |
| `--fs-sm` | 13 / 20 / 400 | 表格正文、说明、详情列表 |
| `--fs-md` | 14 / 22 / 400 | 正文、按钮、导航 |
| `--fs-lg` | 16 / 24 / 600 | 面板标题 h2 |
| `--fs-xl` | 22 / 30 / 650 | 页面标题 h1 |
| `--fs-kpi` | 30 / 36 / 600，mono，`tabular-nums`，letter-spacing −.02em | KPI 数值 |
| `--fs-kpi-unit` | 14 / 20 / 500，`--text-3` | KPI 单位（`/s`、`%`、`ms`） |

所有数字（KPI、表格数值列、坐标轴、详情列表里的 mono 值）统一 `font-variant-numeric: tabular-nums`。

### 4.5 间距、圆角、层级、动效

```css
--space-1: 4px;  --space-2: 8px;  --space-3: 12px; --space-4: 16px;
--space-5: 20px; --space-6: 24px; --space-8: 32px;           /* density 8：8–32 档 */
--radius-sm: 6px; --radius-md: 10px; --radius-lg: 14px; --radius-pill: 999px;
--sidebar-w: 232px; --sidebar-rail: 64px; --topbar-h: 56px;
--row-h: 40px;                                                /* 表格行高 */
--ease: cubic-bezier(.2, .8, .2, 1);
--dur-1: 120ms; /* 悬停、按下 */ --dur-2: 180ms; /* 展开、切换 */ --dur-3: 240ms; /* 视图淡入 */
--z-topbar: 20; --z-skip: 50;
```

暗色下卡片层级用“提亮表面 + 1px 内高光”表示，亮色下用浅阴影表示。

### 4.6 图标

- 来源：Phosphor Icons（MIT，Regular 线框风格），技能默认推荐的图标库。把需要的路径复制成 `<svg><symbol id="i-…" viewBox="0 0 256 256">` 精灵，放在 `index.html` 的 `<body>` 开头并设 `hidden`。不安装 npm 包。在 `index.html` 注释里保留 MIT 版权声明。
- 使用方式：JS 里写一个 `icon(name)` 辅助函数，用 `createElementNS` 创建 `<svg class="icon" aria-hidden="true"><use href="#i-name"/></svg>`。精灵本身是同一文档内的引用，不受 `img-src` 限制。
- 尺寸令牌：`--icon-sm: 16px`（表格、徽章）、`--icon-md: 20px`（导航、按钮）、`--icon-lg: 24px`（健康条、空态）。统一用 `fill: currentColor`。

| 语义 | Phosphor 图标 | 语义 | Phosphor 图标 |
| --- | --- | --- | --- |
| 概览 | `squares-four` | 刷新 | `arrow-clockwise` |
| 节点 | `hard-drives` | 暂停 / 继续 | `pause` / `play` |
| 仓库 | `git-branch` | 断开 | `sign-out` |
| 数据库 | `database` | 主题 | `sun` / `moon` |
| 请求与错误 | `pulse` | 搜索 | `magnifying-glass` |
| 正常 / 就绪 | `check-circle` | 警告 / 未就绪 | `warning` |
| 故障 | `x-circle` | 未知 | `question` |
| 失联 | `wifi-slash` | 已停止 | `stop-circle` |
| 时间 / 新鲜度 | `clock` | 令牌 / 安全 | `shield-check` |
| 写主 | `pencil-simple-line` | 锁 | `lock-simple` |
| 返回 | `arrow-left` | 展开 | `caret-down` |

---

## 5 信息架构与布局

### 5.1 App Shell

```
┌──────────┬──────────────────────────────────────────────────────────────────────────┐
│ ◆ PgVN   │ 服务监控 / 概览        ● 实时 · 3 秒前   [5s|10s|30s|60s] ⏸ ⟳  ☾  ⎋     │ ← Topbar 56px, sticky
│ 仓库服务  ├──────────────────────────────────────────────────────────────────────────┤
│ 监控     │ ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁ 2px 刷新进度条（仅请求中） │
│          │                                                                          │
│ 运行状态  │  PGVN / OPERATIONS                                                      │
│ ▣ 概览   │  服务概览                                                                │
│ ▤ 节点   │  node-a · 实例 3f9c21aa · v0.5.0 · 已运行 3 天 4 小时                    │
│ ⑂ 仓库   │                                                                          │
│ ⛁ 数据库 │  ┌ Health Strip ─────────────────────────────────────────────────────┐  │
│ ∿ 请求   │  │ ✓ 服务就绪 │ ✓ 数据库采样正常 │ ✓ 可写主库 │ ✓ 节点 3/3 │ ◷ 3 秒前 │  │
│          │  └───────────────────────────────────────────────────────────────────┘  │
│          │                                                                          │
│ ─────    │   …视图内容（12 栅格，gap 16）…                                          │
│ ⛨ 只读   │                                                                          │
│ 本标签页 │                                                                          │
│ 令牌     │  PgVN · 原生仓库服务                          只读 · 无持久令牌         │
└──────────┴──────────────────────────────────────────────────────────────────────────┘
```

- **侧栏**（232px，`--nav`）：品牌徽标改成 SVG 几何标志（两个错位方块，象征 PostgreSQL 与 SVN），用纯 SVG 实现，不用字母方块。导航项高 40px，选中项有左侧 3px 主色条、`--primary-soft` 底色和白色文字。底部“只读控制台”区块显示连接状态点（已连接为绿色，未连接为灰色）。
- **顶栏**（56px，sticky，`--surface-1` + 底边线）：
  - 左侧面包屑。
  - 右侧依次是：**实时指示器**（`● 实时 · N 秒前`，已暂停时显示 `‖ 已暂停`，数据过期时为琥珀色 `▲ 样本过期 · N 秒`）、**刷新周期分段控件**（视觉上是分段按钮，底层仍是 `<select id="refresh-interval">`，见 9.1）、暂停、刷新、主题切换、断开。都是 36×36 的图标按钮，有 tooltip 和 `aria-label`。
  - 页脚的 `#sample-meta` 改为 `sr-only`，保留给辅助技术读取；可见的新鲜度信息由顶栏实时指示器和健康条显示。
- **健康条**取代页头右侧的 `#readiness` 徽章，以及原来单独的 `.alert` 横幅。`#readiness` 节点保留，作为健康条的第一段，ID 和文案逻辑不变。`#connection-status` 也保留（单一 `aria-live` 区），视觉上改成健康条下方的一行说明。

### 5.2 健康条

| 段 | 数据来源 | good | warning | danger / neutral |
| --- | --- | --- | --- | --- |
| 服务就绪 | `overview.ready` | ✓ 服务就绪 | ▲ 尚未就绪 | — |
| 数据库采样 | `last_attempt_ok && freshness==="fresh"` | ✓ 采样正常 · 12 ms | ▲ 样本过期 · 47 秒 | ✕ 采样失败 |
| 写主 | `database.writable` | ✓ 可写主库 | ▲ 未确认可写 | ? 未知（数据过期时） |
| 节点 | `nodes_online / nodes_total` | ✓ 3 / 3 在线 | ▲ 2 / 3 在线 | ? 未知 |
| 新鲜度 | `overview.age_seconds` | ◷ 3 秒前 | ▲ >15 秒 | ? 尚无样本 |

在 ≥1100px 宽度下五段等宽排成一行，段与段之间用 1px `--border` 分隔。整条用 `--surface-1` 底色，左侧 4px 色条取五段中最差的状态色。窄屏下改为 2 列网格。每段是 `<li>`，结构为“图标 + 主文字 + 次文字”，整条是 `<ul aria-label="服务健康摘要">`，不设 live（live 播报仍只走 `#connection-status`，避免多个 live 区同时播报）。

### 5.3 栅格与断点

| 断点 | 侧栏 | 内容 padding | KPI 列数 | 图表布局 |
| --- | --- | --- | --- | --- |
| ≥1440 | 232px | 32px，max-width 1600 | 4 | 8 + 4 / 6 + 6 |
| 1100–1439 | 232px | 24px | 4 | 8 + 4 / 6 + 6 |
| 760–1099 | 64px 图标栏（文字改为 tooltip，`aria-label` 不变） | 20px | 2 | 12 / 12 |
| <760 | 隐藏，改为顶部品牌行 + **横向滚动 Tab 条**（`overflow-x:auto`，选中项有下划线，图标 16px + 文字） | 16px | 2（数值 24px） | 12 |
| 375 验收 | 同上，整页不横向溢出，表格在自身容器内横向滚动 | 16px | 2 | 12 |

---

## 6 组件规范

### 6.1 按钮与控件

| 变体 | 样式 | 用途 |
| --- | --- | --- |
| `primary` | `--primary` 底，`--on-primary` 文字，高 40，圆角 `--radius-sm` | 连接监控、搜索 |
| `secondary`（默认） | `--surface-2` 底，`--border-strong` 边 | 分页、清空 |
| `ghost` / `icon` | 透明底，36×36，悬停时 `--surface-2` | 顶栏操作 |
| 分段控件 | 外框 `--surface-2`，选中段 `--surface-3` + 主色文字 | 刷新周期、图表时间窗 |

状态：悬停 `--dur-1` 过渡背景和边框；按下时 `translateY(1px)`（不改变布局尺寸）；`:focus-visible` 为 2px `--focus` 外框，偏移 2px；禁用时透明度 .5 且 `cursor:not-allowed`；忙碌（`aria-busy=true`）时刷新图标旋转 1 圈/800ms，减少动效模式下不旋转，只显示进度条。

输入框：高 40，`--surface-2` 底，`--border-strong` 边，聚焦时边框变主色并加 3px `--primary-soft` 光晕；搜索框左侧放 16px 搜索图标。

### 6.2 徽章（Badge）

`[图标 16] 文字`，高 24，圆角 pill，用 `--{tone}-bg` 底和 `--{tone}` 文字。为适配 forced-colors，加 1px `transparent` 边框（在 forced-colors 下变成 `CanvasText`）。表格内的徽章高 22，字号 12（不再用 10px）。

节点状态映射（逻辑沿用 `nodeStatus()`，只加图标）：

| 文案 | tone | 图标 |
| --- | --- | --- |
| 就绪 | good | `check-circle` |
| 在线 / 就绪未知 | neutral | `question` |
| 未就绪 | warn | `warning` |
| 心跳失联 | warn | `wifi-slash` |
| 已停止 | neutral | `stop-circle` |
| 状态未知 | neutral | `question` |
| 启用 / 禁用（仓库） | good / neutral | `check-circle` / `minus-circle` |

### 6.3 KPI 卡片

```
┌───────────────────────────────────┐
│ ∿ 请求速率                ● 正常  │  ← 标签 12px --text-2 + 右上状态点（可选）
│                                   │
│ 12.4 /s                           │  ← 30px mono 数值 + 14px 单位 --text-3
│ ▲ 18% 较 15 分钟均值              │  ← 差异，12px；方向箭头 + 文字，颜色语义化
│ ╱╲__╱‾‾╲_╱‾‾‾                     │  ← 迷你趋势线 40px 高，series 色，面积 10% 透明
│ 当前实例 · 最近采样区间            │  ← 脚注 12px --text-3
└───────────────────────────────────┘
```

- 结构：`<article class="kpi" data-tone="…">`，内含 `<h3 class="kpi-label">`、`<p class="kpi-value"><span class="num">12.4</span><span class="unit">/s</span></p>`、`<p class="kpi-delta">`、`<svg class="spark" aria-hidden="true">`、`<p class="kpi-note">`。
- 迷你趋势线只是装饰（`aria-hidden`），数值和差异已经以文字形式给出。
- 未知：数值显示 `—`（`--text-3`），旁边 `sr-only` 文字“未知”，脚注说明原因（如“没有完成命令时为未知”），保持现有语义。
- 不同 KPI 的可视化方式：

| 视图 | KPI | 可视化 | 色调规则 |
| --- | --- | --- | --- |
| 概览 | 请求速率 | 迷你趋势线（trends.commands_per_second） | 中性 |
| 概览 | 失败率 | 迷你趋势线（series-2，虚线） | >0 显示 warn，≥5% 显示 bad |
| 概览 | 命令 P95 上界 | **子弹条**：0–2000 ms 刻度，500/1000 ms 处有阈值刻线 | ≤500 good，≤2000 warn，溢出 bad |
| 概览 | 已观测在线节点 | **分段容量条**：每节点一格，在线格为 good，离线格为 warn | 全在线为 good |
| 数据库 | 业务连接租约 | 容量条 leased / limit | ≥80% warn，100% bad |
| 数据库 | 数据库容量 | 纯数值 + MiB/GiB 自动换算 | 中性 |
| 请求 | 当前会话 | 容量条 sessions / 16 | ≥12 warn |
| 详情 | 最新修订 | 数值 `r1234`，mono | 中性 |

“差异”只在窗口内有效样本 ≥4 个时显示，计算方式为 (最新值 − 窗口均值) / 窗口均值。均值为 0 时不显示百分比。阈值是展示层的默认值，写成 JS 常量集中管理，不新增后端配置。

### 6.4 趋势图（重写 `chart()`）

```
命令速率                                         [5 分钟|15 分钟]
最近 15 分钟 · 次 / 秒                           当前 12.4  峰值 18.0
┌────────────────────────────────────────────────────────────┐
│ 18 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ │
│        ╱╲         ╱‾‾╲                         ╭─╮         │
│ 12 ┄┄┄╱┄┄╲┄┄┄┄┄┄┄╱┄┄┄┄╲┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄╱┄┄┄╲┄┄┄●12.4│ ← 末值圆点 + 标签
│  6 ┄┄╱▓▓▓▓╲▓▓▓▓▓▓╱▓▓▓▓▓▓╲▓▓▓▓▓▓    ▓▓▓▓▓▓▓▓▓╱▓▓▓▓▓▓╲▓▓▓▓▓▓│ ← 面积渐变 24%→0%
│  0 ──────────────────────────── ░ 缺口 ░ ─────────────────── │ ← null 段断开 + 浅色斜纹
│    14:05                  14:12                   14:20      │
└────────────────────────────────────────────────────────────┘
▸ 查看趋势数据
```

- **SVG 结构**：在 `<svg>` 内 `<defs>` 里放 `linearGradient`（id 带视图前缀，避免重复），面积用 `fill="url(#…)"`，这是文档内引用，CSP 安全。依次绘制 grid、area、line、last point、crosshair 几个图层。
- **线型区分**：速率用实线 2px，失败率用 `stroke-dasharray: 6 4`，延迟用 `2 4` 点线，资源用实线 1.5px。图例直接写在标题旁，不只靠颜色区分。
- **坐标轴**：Y 轴用 “nice number” 算法取 3–4 个刻度（1/2/5×10ⁿ），替代现在的 `ceil(max*10)/10`。X 轴显示首、中、尾三个时间。字号 12。
- **缺测**：值为 null 时断开折线（保留现有 `flush()` 逻辑），缺口区画浅色斜纹矩形，并标注 “无样本”。
- **悬停 / 焦点读数**：SVG 容器设 `tabindex="0"`，`role="img"`，`aria-label` 写当前值和峰值的摘要。鼠标移动或 ←/→ 键移动垂直准线，浮层显示“时间 · 数值”，浮层文字通过 SVG `<text>` 或 `.chart-readout` DOM 节点用 `textContent` 写入。Home/End 跳到首尾，Esc 隐藏。读数**不**放进 live 区，避免刷屏。
- **时间窗**：分段控件可选 5 分钟 / 15 分钟，在客户端裁剪 `trends`（已有 900 秒数据），不增加请求。所选时间窗保存在 `state.chartWindow`，刷新后保留。
- **刷新时不闪烁**：重建 SVG 时尺寸固定（`viewBox 0 0 720 220`，CSS 高度 220），不做入场动画。
- **数据表**保留 `<details>`，`data-chart` 键不变，`render()` 的展开状态恢复逻辑继续有效。

### 6.5 数据表

- 表头固定：`.table-wrap{max-height:min(70vh,640px);overflow:auto}`，`thead th{position:sticky;top:0;background:var(--surface-2)}`。
- 数值列（修订、计数、租约、次数）**右对齐**，加 `tabular-nums`。`table()` 的列定义增加可选的对齐标记，例如 `{label:"成功",align:"end"}`，字符串列定义保持兼容。
- ID、UUID、实例列用 mono 字体，截断后通过 `title` 属性显示完整值（只是辅助提示，完整值在详情页另有展示）。
- 行高 40，悬停时整行 `--surface-2`，斑马纹不启用（暗色下会显脏）。
- 节点状态列放在第一列，按“故障 → 警告 → 未知 → 正常”排序显示（只在客户端对当前页排序，不影响游标分页）。
- 空态：居中 24px 图标 + 标题 + 说明，替代纯文字。

### 6.6 面板

`<section class="panel">`：`--surface-1` 底、1px `--border` 边、`--radius-md` 圆角、`--shadow` 阴影。头部高 52，左侧放 h2 + 次标题（12px `--text-3`），右侧放工具区（时间窗、计数徽章）。主体 padding 为 `--space-5`。

### 6.7 详情列表

改为两列网格：`dt` 用 `--text-2`、`dd` 右对齐 `--text`，数值和 ID 用 mono；每行之间 1px 虚线分隔。CPU 和工作集两行在文字下方各加一条 4px 细容量条（CPU 为 0–100%；工作集没有上限，不画条，只显示迷你趋势线，数据来自 `series?metric=working_set_bytes`，见 7.1）。

### 6.8 事件时间线（`eventPanel()`）

```
│ ✕  命令执行失败                                    14:21:07
│    svn / commit · monitor-00000000000003a1
│ ▲  数据库暂时不可达                                14:20:51
│    sampler
│ ✓  仓库服务就绪                                    14:02:13
```

左侧 2px 竖线串起各事件节点，节点为 20px 圆形图标，颜色按严重度映射：`*_failed / *_unavailable / http_5xx` 为 bad，`command_denied / http_4xx / *_capacity / reader_role_not_restricted` 为 warn，`ok / business_ready` 为 good，其余为 neutral。时间右对齐，request_id 用 mono 字体并可选中复制。

### 6.9 加载、空态与错误

- **骨架屏**：`navigate()` 切换视图时不再只放一段空态文字，而是按目标视图渲染固定高度的骨架（4 张 KPI 骨架 + 1 个面板骨架），容器设 `aria-busy="true"`，live 区文案保持“正在读取状态”。骨架有缓慢闪光动画，减少动效模式下静止。
- **刷新进度条**：顶栏底部 2px 条，`state.busy` 期间显示不确定进度动画，结束后 `--dur-2` 淡出。
- **过期提示**：去掉内容区里的 `.alert`，由健康条的“数据库采样”段显示 warn，加上 `#connection-status` 文案；同时所有依赖数据库的 KPI 卡片加 `data-stale` 属性，数值降为 `--text-2` 并显示 `◷ 上次样本` 小标签。
- **错误**：请求失败时保留上次渲染的内容，健康条右侧显示红色 “✕ 读取失败 · 重试”按钮（复用 `refresh(true)`）。

### 6.10 登录页

```
┌──────────────────────────────────────────────────────────────┐
│            ┌───────────────────────────────────┐              │
│            │  ⛨  安全访问                        │              │
│            │  连接到 PgVN 监控                   │              │
│            │  使用管理员签发的只读令牌查看运行详情 │              │
│            │                                     │              │
│            │  监控令牌                            │              │
│            │  [ ●●●●●●●●●●●●●●●●●●●  ]           │              │
│            │  粘贴令牌即可连接，无需数据库账号     │              │
│            │  [          连接监控           ]     │              │
│            │  ─────────────────────────────────  │              │
│            │  ◷ 本机监控在线   ⛨ 令牌仅保存在本标签页 │           │
│            └───────────────────────────────────┘              │
│        背景：--bg 上的极淡点阵（CSS radial-gradient，无图片）   │
└──────────────────────────────────────────────────────────────┘
```

单栏居中卡片，最大宽度 440。卡片底部显示两个信任信号：`/health/live` 的结果和令牌保存方式。令牌输入框保持 `type=password`、`autocomplete="off"`，允许粘贴。错误信息 `#token-error` 显示在输入框下方，带 `x-circle` 图标，输入框边框变红并设 `aria-invalid=true`。未连接时健康条显示全灰的“未连接”状态（只有 `#readiness` 一段可见）。

---

## 7 各页面设计

### 7.1 概览（overview）

```
[Health Strip ×5]
[KPI 请求速率 ∿] [KPI 失败率 ∿] [KPI P95 子弹条] [KPI 在线节点 分段条]
┌ 命令速率（大图，8 栏）────────────────┐ ┌ 本机状态（4 栏）─────────┐
│ 面积 + 线 + 末值 + 5/15 分钟切换       │ │ 节点 / 版本 / 启动 / 实例 │
│                                        │ │ CPU ▕██████░░░░▏ 41%      │
│                                        │ │ 工作集 312 MiB  ∿          │
│                                        │ │ 命令 / 失败、收 / 发       │
└────────────────────────────────────────┘ └──────────────────────────┘
┌ 失败率（虚线，6 栏）───────┐ ┌ 平均命令延迟（点线，6 栏）─┐
└────────────────────────────┘ └────────────────────────────┘
┌ 已观测节点（12 栏，表格，状态列在前）──────────────────────────┐
└────────────────────────────────────────────────────────────────┘
```

数据请求：现有 `overview + nodes + trends`。**可选增强**（P3 阶段）：`series?metric=cpu_percent&window=900&step=15` 和 `series?metric=working_set_bytes&window=900&step=15`，每轮多 2 个请求，只在概览页请求，且刷新周期 <15 秒时降为每 3 轮请求一次。这两个端点后端已支持，不需要改后端。

### 7.2 节点（nodes）

KPI：在线 / 总数、就绪数、失联数、最大配置代数（从当前页计算，脚注注明“当前页”）。下方是节点表格。DAV 节点行可展开（沿用 `<details>`），展开后为三栏：请求指标详情列表、HTTP 方法表（按完成数画横向条）、近期错误表。

### 7.3 仓库（repositories）

搜索栏从表格上方的独立表单移到面板头部工具区：搜索图标 + 输入框 + “搜索” + “清空”，ID 不变。表格的名称列使用主色链接，下方附一行 12px `--text-3` 的 UUID 截断；HEAD / 配置、事务 / 锁两列右对齐；“事务 / 锁”非零时用 warn 色数字。分页条为 “第一页 / 下一页 + 当前页 50 条 · 样本上限提示”。

### 7.4 仓库详情（detail）

返回链接改成带 `arrow-left` 图标的 ghost 按钮样式（仍是 `<a>`，`data-focus-key` 不变）。页头下方是仓库名 + 启用徽章 + UUID（mono，可选中）。KPI：最新修订、配置代数、活动事务、业务锁。下方左右两栏（8 + 4）：左栏为近期修订（以时间线呈现 r 号与提交时间）、活动事务、业务锁；右栏为仓库信息详情列表和本机相关错误时间线。

### 7.5 数据库（database）

KPI：数据库容量、业务连接租约（容量条）、控制连接等待、采样耗时（>1000 ms 时 warn）。“连接与查询”详情列表放左侧；右侧 “SQLSTATE 分类”改为**横向条形图 + 表格**：`00 · 成功` 用 good 色，其余用 bad 色，条长按对数比例（成功次数通常远大于错误），旁边标出数值。最下方为活动事务分页表。

### 7.6 请求与错误（requests）

KPI：完成命令、当前会话（容量条 /16）、新发布修订、活动事务。“命令结果”表的成功、权限拒绝、其他失败三列各加一条**堆叠条**（good / warn / bad 三段，宽度为该行占比），同时保留三个数字列。右侧是“监控入口”详情列表，下方是事件时间线。

---

## 8 交互与动效

| 场景 | 效果 | 时长 | 减少动效模式 |
| --- | --- | --- | --- |
| 悬停按钮、行、导航 | 背景和边框渐变 | 120ms | 直接切换 |
| 视图切换 | 内容区 opacity 0→1 | 180ms | 无 |
| 刷新中 | 顶栏 2px 进度条 + 刷新图标旋转 | 持续 | 进度条静态显示，不旋转 |
| 实时指示点 | 每 2 秒一次 6px 光环扩散 | 2s 循环 | 静态圆点；暂停或过期时也停止 |
| KPI 值变化 | 数值背景闪一次 `--primary-soft` 后淡出 | 600ms | 无 |
| `<details>` 展开 | 箭头图标旋转 90° | 180ms | 直接切换 |

不做数字滚动动画，避免 5 秒刷新一次时持续跳动分散注意力。

---

## 9 可访问性与实现细节

### 9.1 保留并加强

- 保留跳过链接、`#main tabindex=-1`、`render()` 的焦点和展开状态恢复、单一 `role=status` live 区、`aria-pressed` 暂停按钮。
- 刷新周期：原生 `<select>` 视觉上改为分段控件时，必须保留原生控件，或者改用 `role="radiogroup"` 实现完整的方向键支持。**推荐保留 `<select>`**，只改样式（自定义箭头图标），这样改动最小，而且 `configurable_refresh` 检查不受影响。
- 图标按钮必须有可见 tooltip（CSS `:hover/:focus-visible` 显示 `data-tip` 伪元素文字，`::after` 的 `content: attr(data-tip)` 不违反 CSP）以及 `aria-label`。
- 主题切换按钮：`aria-pressed` 表示是否为暗色，`aria-label="切换为亮色主题"` 或 `"切换为暗色主题"`。
- 点击目标：桌面端 ≥36×36（顶栏图标按钮），表单和主操作 ≥40；<760px 时 ≥44。
- `@media (forced-colors: active)`：徽章、卡片、健康条加 `1px solid CanvasText` 边框；图表线条用 `stroke: CanvasText`，面积隐藏；聚焦外框用 `Highlight`。
- `@media (prefers-reduced-motion: reduce)`：沿用现有的全局规则，并额外去掉骨架闪光和实时光环。

### 9.2 安全

- 所有新组件一律用 `textContent` 或 `createElementNS` 创建，**不使用 `innerHTML`**（包括图标，统一用 `<use href>`）。
- 不出现 `style` 属性。图表坐标、容量条宽度等动态值写在 SVG 几何属性上（`x`、`width`、`points`），或者通过 `element.style.setProperty('--pct', …)` 设置。后者属于 CSSOM，不受 `style-src` 对内联属性的限制，但实施时要在 Edge/Chrome 中确认没有 CSP 报告。**首选 SVG 属性方案**。
- 不引入任何外部 URL。

### 9.3 主题偏好不持久化

为了让 `no_persistent_token` 检查（“页面源码不使用持久存储”）保持一目了然，主题采用：

1. 默认跟随 `prefers-color-scheme`，并监听它的变化。
2. 手动切换只保存在内存的 `state.theme` 中，在 `<html>` 上设置 `data-theme`。重新加载后回到系统偏好。

如果之后需要记住主题，可以单独评审是否在 `localStorage` 中只写入 `pgvn-monitor-theme` 这一个非敏感键，同时修改 M4 检查的描述。本方案不做。

---

## 10 实施方案

### 10.1 文件改动

| 文件 | 改动 |
| --- | --- |
| `index.html` | 插入 SVG 精灵（约 25 个 symbol，约 12 KB）；重排侧栏和顶栏（增加实时指示器、主题按钮、进度条）；新增 `#health` 健康条容器；登录区改为单栏卡片；`#sample-meta` 改为 `sr-only`；保留第 2 节列出的所有 ID |
| `monitor.css` | 改为**可读的格式化源码**；按“令牌（dark / light / system）→ 基础 → Shell → 组件 → 视图 → 响应式 → 偏好设置媒体查询”分节重写；预计 25–35 KB |
| `monitor.js` | 新增 `icon()`、`kpi()`（取代 `stat()`）、`spark()`、`meter()`、`bullet()`、`healthStrip()`、`skeleton()`、`theme` 控制；`chart()` 增加渐变、末值、线型、键盘读数和时间窗；`table()` 支持对齐标记和 `title`；`eventPanel()` 改为时间线；`render()` 和 `navigate()` 的数据流、epoch 和焦点逻辑**不变**；预计 40–50 KB |

三个文件都远低于 256 KiB 上限。不改动 C 代码、构建脚本或 API。

### 10.2 分阶段

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| P1 基础 | 令牌、双主题、Shell（侧栏、顶栏、实时指示器）、图标精灵、按钮、输入框、徽章 | 两种主题下所有视图都能正常显示，控制台没有 CSP 报告 |
| P2 状态与 KPI | 健康条、KPI 卡片（迷你趋势线、子弹条、容量条）、过期态、骨架屏、进度条 | stop-peer / block-db 场景下健康条和 KPI 状态正确 |
| P3 图表与表格 | 新 `chart()`（时间窗、键盘读数）、固定表头、数值右对齐、事件时间线、SQLSTATE 条形图、命令结果堆叠条；可选资源趋势 | 键盘能读到每个点；数据表展开状态在刷新后保留 |
| P4 打磨与验收 | 响应式（64px 图标栏、移动端 Tab 条）、forced-colors、减少动效、375px；重跑 M4 门禁和浏览器验收，更新证据 | 见 10.3 |

### 10.3 验证

1. 构建：`python scripts/pgvn/build.py`（内部会调用 `embed_web` 重新生成 `pgvn_web.c`）。
2. 自动门禁：`python tests/pgvn/run_gates.py --milestone M4`（包含 `test_m4_smoke` 对 `/monitor/monitor.css`、`/monitor/monitor.js` 的检查）。
3. 浏览器验收：`python tests/pgvn/monitor_browser_fixture.py`，按 [M4 文档“重做浏览器验收”](../pgvn/M4-monitor.zh-CN.md) 的 15 项检查逐项实测，每项都要在**暗色和亮色**下各确认一次。另外增加以下观察记录（写入 observations）：
   - 两种主题下的对比度抽查（DevTools Accessibility 面板）；
   - Windows 高对比度主题（forced-colors）下徽章、健康条、图表可辨；
   - 减少动效模式下没有动画；
   - 图表能用 ←/→/Home/End 读数；
   - 控制台没有 CSP violation；
   - 恶意仓库名在搜索结果、表格 `title` 提示和详情页中都只显示为文本。
4. 证据：资源 SHA-256 会变化，需要生成新的 `m4-browser-*.json`，并更新 `docs/pgvn/evidence/m4*.json` 中的资源摘要（按 M4 文档里的 PowerShell 片段操作）。

---

## 11 风险与取舍

| 风险 | 对策 |
| --- | --- |
| 改版导致已通过的 M4 浏览器证据失效 | 预期之内。P4 统一重跑一次，不在中途反复取证 |
| 每轮请求增多（可选的 series 请求） | 放在 P3，作为可选项；默认降频；不改变非概览视图的请求数 |
| 不能使用 Web 字体，各机器系统字体不同 | 字体栈优先 Windows 自带字体，Linux 或 macOS 浏览器回落到 system-ui；数字统一用 tabular-nums，保证对齐 |
| `element.style.setProperty` 是否受 CSP 影响有争议 | 优先用 SVG 几何属性；如果用 CSSOM，在 P1 先实测 |
| 暗色下状态色饱和度过高，显得刺眼 | 状态底色只用 14% 透明度；纯色只用于图标和文字 |
| 64px 图标栏的可发现性 | 图标都有 tooltip，`aria-label` 保持中文全名；≥1100px 时恢复完整侧栏 |
| 客户端排序与游标分页的语义冲突 | 只对当前页排序，并在表头注明“当前页按状态排序” |

---

## 12 本次实施与验证

已按本方案完成三个资源的改版，保留既有 ID、API 请求、CSP、令牌、游标分页和视图代数语义。六个视图在两种主题下均实测；DAV 明细保留原生 details，刷新后恢复展开、焦点及表格独立滚动位置。新增的本地样本老化时钟和错误重绘均检查当前视图代数，避免慢请求与切换页面同时发生时使用上一页数据。断开、撤销和重载均清空内容、健康摘要、搜索词和令牌输入。

实施取舍：暗色控件边界从 #5A6B8C 调为 #607294，修正其在输入框底色上不足 3:1 的对比度；SVG 图表根据实际容器宽度调整 viewBox，保持窄屏文字 12px；5/15 分钟切换仅裁剪已有样本，没有增加可选的资源 series 请求。28 个 Phosphor Regular 图标来自官方仓库固定提交 2b75f3ad12b420c9504ef05df8d2564a28f8500e，MIT 声明随精灵内嵌。

最终资源大小为 HTML 18,290、CSS 25,874、JS 55,871 字节。四个主产物中只有内嵌页面的 pgvnserve.exe 摘要变化；monitor_probe.exe 也随内嵌资源更新，其余运行库、管理工具、DAV/授权模块及 Apache 依赖摘要与 M5 基线相同。

Release 构建、JavaScript 语法检查、静态契约与 38 项对比度计算通过。完整 M4 门禁 `m4-297b2d21dc76` 八组通过，耗时 400.188 秒，包含 M0–M3 回归。真实浏览器记录 `m4-browser-redesign-713af12243a2` 的 15 项及 `m5-browser-redesign-713af12243a2` 的 6 项通过；另实测图表键盘/时间窗、375px/900px/横屏、延迟响应下的两条视图竞争路径和减少动效计算样式。证据见 [本次固定记录](../pgvn/evidence/m4-monitor-redesign.json)。

P1–P3 和 P4 本机可执行检查已完成。当前浏览器未开放媒体模拟且原生桌面控制不可用，Windows forced-colors 实际视觉检查仍待补；已有 CSS 适配和源码检查，不将其计为视觉通过。对比度采用 sRGB 计算及实际 CSS 颜色核对，没有声称运行 DevTools Accessibility 面板。

预览：[暗色](../pgvn/images/monitor-night-ops-dark.jpg)、[亮色](../pgvn/images/monitor-night-ops-light.jpg)。截图为隔离测试环境真实页面。节点按实例观测，重启前的旧实例保留到遥测清理；本轮恢复检查显示 3 / 5 在线。

---

## 13 第二轮打磨（2026-10-16）

在已有 Night Ops 页面上继续优化，产品改动仅 `monitor.js`、`monitor.css`，API、路由、CSP 和令牌契约不变。

- 心跳年龄加入浏览器端经过时间；暂停后不继续显示过期的就绪状态。完整节点页的健康条与列表使用同一年龄判断，有界摘要明确标为“样本在线”；超过 15 秒未收到响应的服务入口显示未知。
- 表格增加有名称的键盘滚动区域与隐藏标题。节点版本和心跳时间保持单行，375px 下主表行高约 63–65px；标题定位限制在自身滚动容器，展开 DAV 详情不撑宽整页。
- 时间窗选中项增加下划线，窄屏与图表控件焦点框移到边界内侧。方向键读数同步更新图表可访问名称，缺样本数值保持未知；图表 viewBox 随实际窄屏宽度调整。

最终 Release 的完整 M5 门禁 `m5-9cc4f3a5b58d` 十组通过，耗时 551.219 秒；包含完整 M4 `m4-f80d7e26b175` 八组和 M0–M3 回归。新浏览器记录为 `m4-browser-polish-f7fa9e9c4686`（15 项）和 `m5-browser-polish-f7fa9e9c4686`（6 项），包含双主题、实际 375×812、暂停计时、数据库故障/恢复、失联/新实例、撤销及重载检查。38 项计算对比度、静态契约和 JS 语法通过。原始发现、修复、源码/产物摘要和范围见 [第二轮证据](../pgvn/evidence/m4-monitor-polish.json)。

预览：[暗色桌面](../pgvn/images/monitor-polish-dark.jpg)、[亮色窄屏](../pgvn/images/monitor-polish-light.jpg)。相对 `698e7bb` 冻结构建，五个产品中仅内嵌页面的 `pgvnserve.exe` 摘要改变。旧部署 ZIP 不改标为本轮构建。

后续仍需实际 Windows 高对比度视觉验收，以及同页多个 DAV 历史实例时内层表格的焦点恢复边界检查；本次自动刷新焦点验证覆盖节点主表。M6/M7 继续按实施计划推进。

---

## 14 多实例表格焦点（2026-10-16）

已复现并修复：两个同时在线的 DAV 实例展开方法表/错误表后，原焦点键仅含视图、游标和列名，自动刷新会将第二个实例的焦点恢复到第一个。现在复用包含完整实例 ID 的滚动键，并在 DOM 归属确定后生成焦点键；详情组带实例名称。测试 fixture 增加 `--dav --dav-peer`，默认行为保持原样。

复现入口：`python tests/pgvn/monitor_browser_fixture.py --dav --dav-peer`。展开两组 DAV 详情，聚焦第二组内表，保持页面可见并等到采样记录改变。旧构建实际发生串位；新构建在桌面/375px、明暗主题和保留历史实例场景五组通过。焦点所属工作进程失联、内表消失后，焦点回到 `main`，不会移到仍在线实例。原始失败和修复后记录见 [焦点组件证据](../pgvn/evidence/m4-monitor-focus.json)。

本轮验收状态为 **PARTIAL**。M5 浏览器六项通过；M4 的令牌撤销组合项保留待验：清空和已撤销令牌重连的焦点已观察，直接自动撤销瞬间尚未观察。额外十分钟只读测试令牌的创建被自动审批拒绝，正在等待明确授权，没有创建新令牌。M4 门禁保持原断言，实际提前拒绝这份部分证据，启动服务端套件数为零。没有将旧完整门禁改标为当前构建通过。

原始截图仅保留作诊断，视口切换后的抓取未覆盖焦点区域，不计视觉通过。实际 Windows 高对比度与本轮完整门禁仍待补；M6/M7 继续按实施计划推进。

---

## 附录 A：ui-ux-pro-max 查询记录

| 查询 | 领域 | 采用结论 |
| --- | --- | --- |
| `infrastructure monitoring ops dashboard database service` `--design-system --density 8 --motion 3 --variance 4` | design-system | Real-Time / Operations 模式；Dark Mode (OLED)；状态三色；反模式：加载慢的仪表盘、装饰性图表、隐藏错误状态；8–32 间距档 |
| `dark mode dashboard data-dense` | style | Data-Dense Dashboard：12 栅格、KPI 卡片行、固定表头、行高约 36–40、侧栏 240、顶栏 56 |
| `devops monitoring observability` → 无匹配，重试 `analytics dashboard developer tool` | color | 参考 Developer Tool（`#0F172A` 底、`#94A3B8` 弱化文字）与 Analytics Dashboard（蓝色数据 + 琥珀高亮）；最终配色按第 4.3 节自行核算 |
| `real-time monitoring time series` | chart | 用折线 + 高亮标异常；必须能暂停；用线型区分系列；提供数据表兜底；<1000 点用 SVG |
| `KPI sparkline single value` | chart | 有阈值的指标用 Bullet / Gauge，数值和阈值都要有文字 |
| `live status update stale data` | ux | 状态播报只用一个原子化的 `role=status`，不要让每个徽章各自成为 live 区 |
| `skeleton loading placeholder` | ux | 用稳定的骨架和 `aria-busy`，避免加载指示闪烁 |
| `color not only indicator status` | ux | 用图标和文字配合颜色，不能只靠红绿区分 |
| `monospace dashboard technical system font` | typography | Fira Code + Fira Sans（因 CSP 换成 Cascadia Mono + Segoe UI） |
| `server database status activity` | icons | Phosphor：database、pulse、check-circle、warning、x-circle、clock、info |
| `data table dense sticky header` `--stack html-tailwind` | stack | 无匹配（项目不用 Tailwind），表格规范按通用做法制定 |
| `responsive dashboard horizontal overflow` | ux | 宽表在自身容器内滚动；长标识不撑宽整页；窄屏结果以实际布局测量为准 |
