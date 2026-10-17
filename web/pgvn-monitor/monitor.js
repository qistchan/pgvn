"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const labels = { overview: "概览", nodes: "节点", repositories: "仓库", database: "数据库", requests: "请求与错误", detail: "仓库详情" };
  const titles = { overview: "服务概览", nodes: "节点状态", repositories: "仓库状态", database: "数据库状态", requests: "请求与错误", detail: "仓库详情" };
  const state = { token: "", view: "overview", busy: false, paused: false, timer: null, cursor: "", next: null, overview: null, rows: null, trends: null, events: null, repository: "", search: "", epoch: 0, refreshSeconds: 5, renderedEpoch: -1, theme: null, chartWindow: 900, chartCursors: {}, chartObservers: [], kpiValues: {}, fetchedAt: 0, updateFailed: false, ageExpired: false, nodeStatusKey: "", clock: null };
  const number = value => value == null ? "未知" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);
  const time = value => value != null && value !== "" && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "未知";
  const element = (tag, className, text) => { const e = document.createElement(tag); if (className) e.className = className; if (text != null) e.textContent = text; return e; };
  const searchForm = $("search-form");
  const SVG = "http://www.w3.org/2000/svg";
  const limits = { errorWarn: 0, errorBad: 5, latencyGood: 500, latencyTick: 1000, latencyMax: 2000, leaseWarn: .8, sessionWarn: .75, sampleAge: 15 };
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  const svgElement = (tag, attributes = {}) => { const e = document.createElementNS(SVG, tag); for (const [key, value] of Object.entries(attributes)) e.setAttribute(key, String(value)); return e; };
  function icon(name) { const svg = svgElement("svg", { "aria-hidden": "true", class: "icon", viewBox: "0 0 256 256" }); svg.append(svgElement("use", { href: `#i-${name}` })); return svg; }
  function buttonIcon(id, name, label) { const button = $(id); button.replaceChildren(icon(name)); button.setAttribute("aria-label", label); button.dataset.tip = label; }
  function syncTheme() {
    const dark = state.theme === "dark" || (!state.theme && scheme.matches);
    buttonIcon("theme", dark ? "sun" : "moon", dark ? "切换为亮色主题" : "切换为暗色主题");
    $("theme").setAttribute("aria-pressed", String(dark));
  }
  function status(text, tone = "") { const node = $("connection-status"); if (node.textContent !== text) node.textContent = text; node.className = `connection-status ${tone}`; }
  function badge(text, tone, name) {
    const names = { "就绪": "check-circle", "心跳失联": "wifi-slash", "已停止": "stop-circle", "未就绪": "warning", "状态未知": "question", "启用": "check-circle", "禁用": "minus-circle" };
    const e = element("span", `badge ${tone}`); e.append(icon(name || names[text] || (tone === "good" ? "check-circle" : tone === "warning" ? "warning" : "question")), element("span", "", text)); return e;
  }
  function nodeInfo(node) {
    const sample = state.rows;
    if (state.updateFailed || state.ageExpired || !sample || !sample.last_attempt_ok || sample.freshness !== "fresh" || !Number.isFinite(node.age_seconds) || node.age_seconds < 0) return { text: "状态未知", tone: "neutral", rank: 2 };
    if (node.metrics?.stopped === true) return { text: "已停止", tone: "neutral", rank: 2 };
    if (node.age_seconds + (sample.age_seconds || 0) + elapsedSinceFetch() > limits.sampleAge) return { text: "心跳失联", tone: "warning", rank: 0 };
    if (node.metrics?.ready === false) return { text: "未就绪", tone: "warning", rank: 1 };
    return node.metrics?.ready === true ? { text: "就绪", tone: "good", rank: 3 } : { text: "在线 / 就绪未知", tone: "neutral", rank: 2 };
  }
  function elapsedSinceFetch() { return Math.max(0, (performance.now() - state.fetchedAt) / 1000); }
  function nodeStatusKey() {
    return ["overview", "nodes"].includes(state.view) ? (state.rows?.items || []).map(n => `${n.instance_id}:${nodeInfo(n).text}`).join("|") : "";
  }
  function tick() {
    if (!state.token || !state.overview) return;
    const age = state.overview.age_seconds, expired = age != null && age + elapsedSinceFetch() > limits.sampleAge;
    const changed = expired !== state.ageExpired;
    state.ageExpired = expired;
    // Heartbeats may expire before the database sample, including while paused.
    if (state.renderedEpoch === state.epoch && (changed || nodeStatusKey() !== state.nodeStatusKey)) render();
    else healthStrip();
  }
  function timestamp(value) { const e = element("time", "nowrap", time(value)); if (value != null && value !== "" && Number.isFinite(new Date(value).getTime())) e.dateTime = new Date(value).toISOString(); return e; }
  function nodeStatus(node) { const s = nodeInfo(node); return badge(s.text, s.tone); }
  function panel(title, subtitle) { const p = element("section", "panel"), heading = element("div", "panel-heading"); heading.append(element("h2", "", title)); if (subtitle) heading.append(element("small", "", subtitle)); p.append(heading); return p; }
  function details(items) { const dl = element("dl", "detail-list"); for (const [title, value, mono] of items) { const dd = element("dd", mono ? "mono" : ""); if (value instanceof Node) dd.append(value); else dd.textContent = value == null ? "未知" : value; dl.append(element("dt", "", title), dd); } return dl; }
  function empty(title, text) { const box = element("div", "empty"); box.append(icon("info"), element("strong", "", title), element("span", "", text)); return box; }
  function skeleton() {
    const box = element("div", "skeleton"), stats = element("div", "stats"); box.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 4; i++) stats.append(element("div", "skeleton-block")); box.append(stats, element("div", "skeleton-block")); return box;
  }
  function table(columns, rows, title = "数据表") {
    const wrap = element("div", "table-wrap"), t = element("table"), head = element("thead"), tr = element("tr");
    wrap.dataset.scrollKey = JSON.stringify([state.view, state.repository, state.search, state.cursor, columns.map(c => typeof c === "string" ? c : c.label)]);
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", `${title}，可滚动表格`);
    const caption = element("caption", "sr-only", `${title}。可聚焦表格区域，使用方向键滚动。`); t.append(caption);
    for (const column of columns) { const th = element("th", column.align === "end" ? "align-end" : "", typeof column === "string" ? column : column.label); th.scope = "col"; tr.append(th); } head.append(tr); t.append(head);
    const body = element("tbody");
    for (const row of rows) {
      const r = element("tr"); let expanded;
      row.forEach((cell, index) => {
        const td = element("td", columns[index]?.align === "end" ? "align-end" : "");
        if (cell instanceof Node) {
          td.append(cell);
          if (cell.matches("details.node-details")) {
            const content = cell.querySelector(".dav-detail-body");
            if (content) {
              expanded = element("tr"); const span = element("td", "expanded-cell"); span.colSpan = columns.length; span.append(content); expanded.append(span); expanded.hidden = !cell.open;
              content.id = `dav-detail-${cell.dataset.chart}`; cell.querySelector("summary").setAttribute("aria-controls", content.id);
              const extra = expanded; cell.pgvnExpandedRow = extra; cell.addEventListener("toggle", () => { extra.hidden = !cell.open; });
            }
          }
        } else { td.textContent = cell == null ? "未知" : String(cell); td.title = td.textContent; }
        r.append(td);
      });
      body.append(r); if (expanded) body.append(expanded);
    }
    t.append(body); wrap.append(t); return wrap;
  }
  const endColumn = label => ({ label, align: "end" });
  function mono(value, short = false) { const e = element("span", "mono", short ? String(value).slice(0, 8) : value); e.title = value; return e; }
  function setSignal(id, text, tone, name) { const e = $(id); e.className = `health-value ${tone}`; e.replaceChildren(icon(name), element("span", "", text)); }
  function healthStrip() {
    const o = state.overview, connected = Boolean(o && state.token);
    $("health").classList.toggle("is-disconnected", !connected); $("session-label").textContent = connected ? "已连接 · 只读" : "未连接";
    if (!connected) { delete document.body.dataset.connected; $("health").dataset.tone = "neutral"; setSignal("readiness", "未连接", "neutral", "question"); for (const id of ["health-sample", "health-writer", "health-nodes"]) setSignal(id, "未知", "neutral", "question"); setSignal("health-age", "尚无样本", "neutral", "clock"); $("freshness").className = "freshness neutral"; $("freshness").replaceChildren(icon("clock"), element("span", "", "等待连接")); return; }
    document.body.dataset.connected = "true";
    const d = o.database, fresh = !state.updateFailed && !state.ageExpired && o.last_attempt_ok && o.freshness === "fresh";
    const age = o.age_seconds == null ? null : Math.max(0, o.age_seconds + elapsedSinceFetch());
    const ageText = age == null ? "尚无样本" : `${number(age)} 秒前`;
    const reachable = !state.updateFailed && elapsedSinceFetch() <= limits.sampleAge;
    setSignal("readiness", !reachable ? "状态未知" : o.ready ? "服务就绪" : "尚未就绪", !reachable ? "neutral" : o.ready ? "good" : "warning", !reachable ? "question" : o.ready ? "check-circle" : "warning");
    setSignal("health-sample", fresh ? `采样正常 · ${number(o.sampler_milliseconds)} ms` : o.sampled_at ? `样本过期 · ${ageText}` : "尚无成功样本", fresh ? "good" : "warning", fresh ? "check-circle" : "warning");
    setSignal("health-writer", !fresh || d?.writable == null ? "未知" : d.writable ? "可写主库" : "未确认可写", !fresh || d?.writable == null ? "neutral" : d.writable ? "good" : "warning", !fresh ? "question" : d?.writable ? "pencil-simple-line" : "warning");
    const known = fresh && d?.nodes_online != null && d?.nodes_total != null;
    const completeNodes = state.renderedEpoch === state.epoch && ["overview", "nodes"].includes(state.view) && state.rows?.last_attempt_ok && state.rows.freshness === "fresh" && !state.cursor && !state.rows.next_cursor && !state.rows.truncated && state.rows.items?.length === d?.nodes_total;
    const online = completeNodes ? state.rows.items.filter(n => ["就绪", "未就绪", "在线 / 就绪未知"].includes(nodeInfo(n).text)).length : d?.nodes_online;
    setSignal("health-nodes", known ? `${number(online)} / ${number(d.nodes_total)} ${completeNodes ? "在线" : "样本在线"}` : "未知", known ? online === d.nodes_total ? "good" : "warning" : "neutral", known ? online === d.nodes_total ? "hard-drives" : "warning" : "question");
    setSignal("health-age", ageText, age == null ? "neutral" : age > limits.sampleAge ? "warning" : "good", "clock");
    $("health").dataset.tone = state.updateFailed ? "danger" : !fresh || !o.ready || (known && online < d.nodes_total) || age > limits.sampleAge ? "warning" : "good";
    const text = state.paused ? `已暂停 · ${ageText}` : !fresh || age > limits.sampleAge ? `样本过期 · ${ageText}` : `实时 · ${ageText}`;
    $("freshness").className = `freshness ${state.paused ? "neutral" : fresh && age <= limits.sampleAge ? "good" : "warning"}`;
    $("freshness").replaceChildren(icon(state.paused ? "pause" : fresh ? "pulse" : "warning"), element("span", "", text));
  }
  function meter(value, total, { tone = "good", ticks = [], segments = false } = {}) {
    const svg = svgElement("svg", { class: "meter", viewBox: "0 0 200 16", preserveAspectRatio: "none", "aria-hidden": "true" });
    svg.append(svgElement("rect", { class: "meter-track", x: 0, y: 5, width: 200, height: 6, rx: 3 }));
    if (!Number.isFinite(value) || !Number.isFinite(total) || total <= 0) return svg;
    const ratio = Math.min(1, Math.max(0, value / total));
    if (segments && total <= 40) { const count = Math.max(1, Math.floor(total)); for (let i = 0; i < count; i++) svg.append(svgElement("rect", { class: i < value ? "meter-good" : "meter-warning", x: i * 200 / count, y: 5, width: Math.max(0, 200 / count - 2), height: 6, rx: 1 })); }
    else svg.append(svgElement("rect", { class: `meter-${tone}`, x: 0, y: 5, width: ratio * 200, height: 6, rx: 3 }));
    for (const tick of ticks) svg.append(svgElement("line", { class: "meter-tick", x1: tick / total * 200, x2: tick / total * 200, y1: 2, y2: 14 })); return svg;
  }
  function trendPoints(field) {
    const items = state.trends?.items || [], end = Date.parse(items.at(-1)?.sampled_at);
    return items.filter(p => Number.isFinite(Date.parse(p.sampled_at)) && Date.parse(p.sampled_at) >= end - state.chartWindow * 1000);
  }
  function spark(points, field, series = 1) {
    const svg = svgElement("svg", { class: `spark series-${series}`, viewBox: "0 0 200 40", preserveAspectRatio: "none", "aria-hidden": "true" });
    const values = points.map(p => Number.isFinite(p[field]) && p[field] >= 0 ? p[field] : null), valid = values.filter(v => v != null);
    if (valid.length < 2) return svg;
    const max = Math.max(...valid, 1), pointsPerSegment = []; let segment = [];
    values.forEach((v, i) => { if (v == null) { if (segment.length) pointsPerSegment.push(segment); segment = []; } else segment.push(`${i / Math.max(1, values.length - 1) * 200},${38 - v / max * 34}`); }); if (segment.length) pointsPerSegment.push(segment);
    for (const part of pointsPerSegment) svg.append(svgElement("polyline", { class: "spark-line", points: part.join(" "), ...(series === 2 ? { "stroke-dasharray": "6 4" } : {}) })); return svg;
  }
  function kpi(label, value, note, options = {}) {
    const e = element("article", "kpi"), heading = element("h3", "kpi-label"); e.dataset.tone = options.tone || "neutral";
    if (options.icon) heading.append(icon(options.icon)); heading.append(element("span", "", label));
    const p = element("p", "kpi-value"), unknown = value == null || value === "未知";
    p.append(element("span", "num", unknown ? "—" : value)); if (unknown) p.append(element("span", "sr-only", "未知")); else if (options.unit) p.append(element("span", "unit", options.unit));
    const key = `${state.view}:${label}`, previous = state.kpiValues[key]; if (previous != null && previous !== String(value)) p.classList.add("value-changed"); state.kpiValues[key] = String(value);
    e.append(heading, p);
    if (options.field) {
      const points = trendPoints(options.field), valid = points.map(x => x[options.field]).filter(Number.isFinite), delta = element("p", "kpi-delta");
      const mean = valid.reduce((a, b) => a + b, 0) / valid.length, latest = points.at(-1)?.[options.field];
      if (valid.length >= 4 && mean > 0 && Number.isFinite(latest)) { const diff = (latest - mean) / mean * 100; delta.append(icon(diff >= 0 ? "arrow-up-right" : "arrow-down-right"), element("span", "", `${diff >= 0 ? "+" : ""}${number(diff)}% 较 ${state.chartWindow / 60} 分钟窗口均值`)); }
      else delta.textContent = "正在收集窗口趋势";
      e.append(delta, spark(points, options.field, options.series));
    } else if (options.meter) e.append(meter(...options.meter));
    e.append(element("p", "kpi-note", note));
    if (options.stale) { e.dataset.stale = "true"; const last = element("p", "stale-label"); last.append(icon("clock"), element("span", "", "上次样本")); e.append(last); }
    return e;
  }
  function stat(label, value, note) { return kpi(label, value, note); }
  function resultBar(values, logarithmic = false) {
    const svg = svgElement("svg", { class: logarithmic ? "sql-bar" : "result-bar", viewBox: "0 0 180 8", preserveAspectRatio: "none", "aria-hidden": "true" });
    svg.append(svgElement("rect", { class: "meter-track", width: 180, height: 8, rx: 3 })); let offset = 0;
    const sum = values.reduce((s, v) => s + v, 0); if (!sum) return svg;
    values.forEach((value, i) => { const width = value / sum * 180; svg.append(svgElement("rect", { class: `meter-${["good", "warning", "danger"][i]}`, x: offset, width, height: 8 })); offset += width; }); return svg;
  }
  async function api(path) {
    const controller = new AbortController(), deadline = setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(`/api/monitor/v1/${path}`, { headers: { Authorization: `Bearer ${state.token}` }, cache: "no-store", signal: controller.signal });
      if (response.status === 401) { const error = new Error("令牌无效、已过期或已撤销，请重新连接。"); error.auth = true; throw error; }
      if (response.status === 409) { const error = new Error("状态已更新，正在返回第一页。"); error.cursor = true; throw error; }
      if (!response.ok) {
        const failure = await response.json().catch(() => null), messages = { not_found: "找不到这个仓库或状态入口。", repository_not_in_snapshot: "仓库尚未进入本次有界快照，请稍后重试。", invalid_query: "查询条件不符合要求，请调整后重试。" };
        throw new Error(messages[failure?.error?.code] || `读取失败（HTTP ${response.status}），可稍后重试。`);
      }
      return await response.json();
    } finally { clearTimeout(deadline); }
  }
  function schedule() { clearTimeout(state.timer); if (!state.paused && state.token) state.timer = setTimeout(refresh, (document.hidden ? Math.max(30, state.refreshSeconds) : state.refreshSeconds) * 1000); }
  function disconnect(message) {
    state.token = ""; $("token").value = ""; clearTimeout(state.timer); clearInterval(state.clock); state.clock = null;
    state.chartObservers.forEach(observer => observer.disconnect()); state.chartObservers = []; state.chartCursors = {}; state.kpiValues = {}; state.updateFailed = false; state.search = ""; $("repository-search").value = "";
    $("content").before(searchForm); state.overview = null; state.rows = null; state.trends = null; state.events = null; state.next = null;
    $("content").hidden = true; $("content").replaceChildren(); $("login").hidden = false; $("logout").hidden = true; $("search-form").hidden = true; state.epoch++;
    $("readiness").textContent = "未连接"; $("readiness").className = "badge neutral"; if (message) status(message, "warning"); $("subtitle").textContent = "连接监控，查看节点与仓库的运行状态。"; $("sample-meta").textContent = "尚未取得状态样本"; $("health-retry").hidden = true; healthStrip(); $("token").focus({ preventScroll: true }); $("token-form").reset();
  }
  async function refresh(force = false) {
    clearTimeout(state.timer); if (!state.token || state.busy) return;
    state.busy = true; document.body.dataset.busy = "true"; $("health-retry").hidden = true; $("refresh").setAttribute("aria-disabled", "true"); $("refresh").setAttribute("aria-busy", "true");
    const view = state.view, epoch = state.epoch;
    try {
      const list = view === "database" ? "transactions" : view === "overview" ? "nodes" : view === "detail" ? `repos/${state.repository}` : view === "repositories" ? "repos" : view;
      const overview = await api("overview");
      if (state.overview && state.overview.instance_id !== overview.instance_id) state.cursor = "";
      // Refresh the snapshot prefix while preserving the visible page offset.
      // A human can read a page for longer than the five-second sampling period.
      const cursor = state.cursor ? `${overview.sample_id}:${state.cursor.split(":").at(-1)}` : "";
      const query = ["nodes", "repos", "transactions"].includes(list) ? `?limit=50${cursor ? `&cursor=${cursor}` : ""}` : "";
      const search = list === "repos" && state.search ? `&q=${encodeURIComponent(state.search)}` : "";
      const [rows, trends, events] = await Promise.all([api(list + query + search), view === "overview" ? api("trends") : null, view === "requests" ? api("events") : null]);
      if (!state.token || epoch !== state.epoch) return;
      Object.assign(state, { overview, rows, trends, events, next: rows.next_cursor, fetchedAt: performance.now(), updateFailed: false, ageExpired: overview.age_seconds != null && overview.age_seconds > limits.sampleAge });
      const firstConnection = !$("login").hidden;
      $("login").hidden = true; $("content").hidden = false; $("logout").hidden = false; $("search-form").hidden = view !== "repositories";
      if (firstConnection) $("main").focus({ preventScroll: true });
      const stale = overview.freshness !== "fresh" || !overview.last_attempt_ok;
      const ready = overview.ready;
      $("readiness").textContent = ready ? "服务就绪" : "尚未就绪"; $("readiness").className = `badge ${ready ? "good" : "warning"}`;
      $("subtitle").textContent = `${overview.node_id} · 实例 ${overview.instance_id.slice(0, 8)} · ${overview.process.version}`;
      $("sample-meta").textContent = `采样于 ${time(overview.sampled_at)} · ${overview.age_seconds == null ? "尚无样本" : `${number(overview.age_seconds)} 秒前`}`;
      status(stale ? "数据库状态暂时无法更新。保留上次成功样本，本机进程指标仍可查看。" : state.paused ? "自动刷新已暂停，可手动刷新。" : ready ? `服务运行正常。每 ${state.refreshSeconds} 秒自动刷新，只读访问。` : "监控已连接，仓库服务尚未就绪。", stale ? "warning" : "");
      render(); if (!state.clock) state.clock = setInterval(tick, 1000);
    } catch (error) {
      if (error.auth) { disconnect(error.message); $("token-error").textContent = error.message; $("token-error").hidden = false; $("token").setAttribute("aria-invalid", "true"); }
      else if (error.cursor) { state.cursor = ""; status(error.message); setTimeout(refresh, 0); }
      else { state.updateFailed = true; status(error.name === "AbortError" ? "连接超时。当前页面保留上次读取结果。" : error.message, "danger"); $("health-retry").hidden = false; if (state.renderedEpoch !== epoch) { $("content").before(searchForm); $("content").replaceChildren(empty("暂时无法读取状态", "请使用刷新按钮重试。")); } else if (state.overview && state.renderedEpoch === state.epoch) render(); healthStrip(); }
    } finally { state.busy = false; delete document.body.dataset.busy; $("content").removeAttribute("aria-busy"); $("refresh").removeAttribute("aria-disabled"); $("refresh").removeAttribute("aria-busy"); if (epoch !== state.epoch && state.token) setTimeout(() => refresh(true), 0); else schedule(); }
  }
  function chart(title = "命令速率", field = "commands_per_second", unit = "次 / 秒", series = 1) {
    const box = panel(title, `最近 ${state.chartWindow / 60} 分钟 · ${unit}`), points = trendPoints(field).sort((a, b) => Date.parse(a.sampled_at) - Date.parse(b.sampled_at));
    const tools = element("div", "chart-window"); tools.setAttribute("role", "group"); tools.setAttribute("aria-label", `${title}时间窗`);
    for (const seconds of [300, 900]) {
      const b = element("button", "", `${seconds / 60} 分钟`); b.type = "button"; b.setAttribute("aria-pressed", String(state.chartWindow === seconds)); b.dataset.focusKey = `window-${field}-${seconds}`;
      b.addEventListener("click", () => { state.chartWindow = seconds; render(); }); tools.append(b);
    }
    box.querySelector(".panel-heading").append(tools);
    const valid = points.filter(p => Number.isFinite(p[field]) && p[field] >= 0);
    if (valid.length < 2) { box.append(empty("正在收集趋势", "足够样本后显示；无完成命令时，失败率和延迟为未知。")); return box; }
    const metric = value => Number.isFinite(value) && value >= 0 ? number(value) : "未知";
    const latest = points.at(-1)?.[field], peak = Math.max(...valid.map(p => p[field]));
    const summary = element("div", "chart-summary"); summary.append(element("span", "", `当前 ${metric(latest)} ${unit}`), element("span", "", `峰值 ${number(peak)} ${unit}`)); box.append(summary);
    const plot = element("div", `chart-plot series-${series}`); plot.tabIndex = 0; plot.dataset.focusKey = `chart-${field}`;
    plot.setAttribute("role", "img"); plot.setAttribute("aria-label", `${title}趋势，当前 ${metric(latest)} ${unit}，峰值 ${number(peak)} ${unit}。使用方向键读取采样点。`);
    plot.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight Home End Escape");
    const svg = svgElement("svg", { class: `chart series-${series}`, "aria-hidden": "true", viewBox: "0 0 720 220" }); plot.append(svg);
    const readout = element("p", "chart-readout", "选择图表，使用 ← / →、Home / End 查看采样值；Esc 隐藏准线。"); readout.id = `readout-${field}`; plot.setAttribute("aria-describedby", readout.id);
    const help = element("p", "chart-help", series === 2 ? "虚线 · 缺口表示无样本；详细数值可展开下方数据表。" : series === 3 ? "点线 · 缺口表示无样本；详细数值可展开下方数据表。" : "实线 · 缺口表示无样本；详细数值可展开下方数据表。");
    box.append(plot, readout, help);
    const raw = element("details", "chart-details"); raw.dataset.chart = field; raw.append(element("summary", "", "查看趋势数据"), table(["采样时间", endColumn(title)], points.map(p => [time(p.sampled_at), metric(p[field])]), `${title}趋势数据`)); box.append(raw);
    const saved = state.chartCursors[field]; let selected = saved ? points.findIndex(p => p.sampled_at === saved.at) : points.length - 1;
    if (selected < 0) selected = points.length - 1;
    let width = 720, crosshair, marker, cursorVisible = saved?.visible === true;
    const start = Date.parse(points[0].sampled_at), end = Date.parse(points.at(-1).sampled_at);
    const rough = Math.max(peak, 1) / 3, power = 10 ** Math.floor(Math.log10(rough)), fraction = rough / power;
    const step = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power, ceiling = step * 3;
    const x = p => 42 + (Date.parse(p.sampled_at) - start) / (end - start || 1) * (width - 102), y = value => 176 - value / ceiling * 144;
    function cursor(index, visible = true) {
      selected = Math.min(points.length - 1, Math.max(0, index)); cursorVisible = visible; const p = points[selected], known = Number.isFinite(p[field]) && p[field] >= 0;
      state.chartCursors[field] = { at: p.sampled_at, visible };
      crosshair?.setAttribute("x1", x(p)); crosshair?.setAttribute("x2", x(p)); crosshair?.setAttribute("visibility", visible ? "visible" : "hidden");
      marker?.setAttribute("cx", x(p)); marker?.setAttribute("cy", known ? y(p[field]) : 176); marker?.setAttribute("visibility", visible && known ? "visible" : "hidden");
      readout.textContent = visible ? `${time(p.sampled_at)} · ${known ? `${number(p[field])} ${unit}` : "无样本"}` : "选择图表，使用 ← / →、Home / End 查看采样值；Esc 隐藏准线。";
      plot.setAttribute("aria-label", `${title}趋势，${visible ? readout.textContent : `当前 ${metric(latest)} ${unit}，峰值 ${number(peak)} ${unit}`}。使用方向键读取采样点。`);
    }
    function draw(actualWidth) {
      width = Math.max(180, actualWidth); svg.setAttribute("viewBox", `0 0 ${width} 220`); svg.replaceChildren();
      const make = (tag, attrs, parent = svg) => { const e = svgElement(tag, attrs); parent.append(e); return e; };
      const defs = make("defs", {}), gradientId = `area-${state.view}-${field}`, gapId = `gap-${state.view}-${field}`;
      const gradient = make("linearGradient", { id: gradientId, x1: 0, x2: 0, y1: 0, y2: 1 }, defs);
      make("stop", { offset: "0%", "stop-color": "currentColor", "stop-opacity": .24 }, gradient); make("stop", { offset: "100%", "stop-color": "currentColor", "stop-opacity": 0 }, gradient);
      const pattern = make("pattern", { id: gapId, width: 8, height: 8, patternUnits: "userSpaceOnUse" }, defs); make("path", { d: "M-2 2L2-2M0 8L8 0M6 10L10 6", class: "chart-grid" }, pattern);
      for (let tick = 0; tick <= 3; tick++) {
        const value = tick * step, at = y(value); make("line", { class: "chart-grid", x1: 42, x2: width - 60, y1: at, y2: at });
        make("text", { x: 36, y: at + 4, "text-anchor": "end" }).textContent = number(value);
      }
      let segment = [];
      function flush() {
        if (!segment.length) return;
        const coordinates = segment.map(p => `${x(p)},${y(p[field])}`).join(" ");
        make("polygon", { class: "chart-area", points: `${x(segment[0])},176 ${coordinates} ${x(segment.at(-1))},176`, fill: `url(#${gradientId})` });
        if (segment.length === 1) make("circle", { class: "chart-dot", cx: x(segment[0]), cy: y(segment[0][field]), r: 3 });
        else make("polyline", { class: "chart-line", points: coordinates, ...(series === 2 ? { "stroke-dasharray": "6 4" } : series === 3 ? { "stroke-dasharray": "2 4" } : {}) });
        segment = [];
      }
      points.forEach((p, index) => {
        if (Number.isFinite(p[field]) && p[field] >= 0) segment.push(p);
        else { flush(); const left = index ? (x(points[index - 1]) + x(p)) / 2 : 42, right = index + 1 < points.length ? (x(p) + x(points[index + 1])) / 2 : width - 60;
          make("rect", { x: left, y: 24, width: Math.max(2, right - left), height: 152, fill: `url(#${gapId})` });
          if (right - left > 65) make("text", { x: (left + right) / 2, y: 44, "text-anchor": "middle" }).textContent = "无样本";
        }
      }); flush();
      const last = points.at(-1);
      if (Number.isFinite(last[field]) && last[field] >= 0) { make("circle", { class: "chart-dot", cx: x(last), cy: y(last[field]), r: 4 }); make("text", { class: "last-label", x: x(last) + 8, y: y(last[field]) + 4 }).textContent = number(last[field]); }
      for (const [at, anchor] of [[start, "start"], [(start + end) / 2, "middle"], [end, "end"]]) make("text", { x: 42 + (at - start) / (end - start || 1) * (width - 102), y: 208, "text-anchor": anchor }).textContent = new Date(at).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
      crosshair = make("line", { class: "chart-crosshair", y1: 24, y2: 176 }); marker = make("circle", { class: "chart-dot", r: 4 }); cursor(selected, cursorVisible);
    }
    plot.addEventListener("keydown", event => { const action = { ArrowLeft: selected - 1, ArrowRight: selected + 1, Home: 0, End: points.length - 1 }; if (event.key === "Escape") { event.preventDefault(); cursor(selected, false); } else if (Object.hasOwn(action, event.key)) { event.preventDefault(); cursor(action[event.key]); } });
    plot.addEventListener("focus", () => cursor(selected));
    plot.addEventListener("pointermove", event => { const rect = svg.getBoundingClientRect(), at = (event.clientX - rect.left) / rect.width * width; let index = 0, distance = Infinity; points.forEach((p, i) => { const next = Math.abs(x(p) - at); if (next < distance) { distance = next; index = i; } }); cursor(index); });
    plot.addEventListener("pointerleave", () => { if (document.activeElement !== plot) cursor(selected, false); });
    draw(720); const observer = new ResizeObserver(entries => { if (plot.isConnected) draw(entries[0].contentRect.width); }); observer.observe(plot); state.chartObservers.push(observer);
    return box;
  }
  function paged(title, columns, rows, emptyTitle) {
    const box = panel(title, `${state.rows?.items?.length || 0} 条 / 当前页${state.view === "nodes" ? " · 当前页按状态排序" : ""}`);
    if (state.view === "repositories") box.append(searchForm);
    if (rows.length) box.append(table(columns, rows, title)); else box.append(empty(emptyTitle, state.rows?.freshness === "unknown" ? "尚未取得数据库样本。" : "最近一次采样没有符合条件的记录。"));
    const pager = element("div", "pager"), buttons = element("div", "pager-buttons");
    pager.append(element("span", "", state.rows?.truncated ? "样本达到 1000 条上限，部分记录未展示。" : "每页最多 50 条，状态基于最近一次采样。"));
    const first = element("button", "", "第一页"); first.dataset.page = "first"; first.disabled = !state.cursor; first.addEventListener("click", () => { state.cursor = ""; refresh(true); });
    const next = element("button", "", "下一页"); next.dataset.page = "next"; next.disabled = !state.next; next.addEventListener("click", () => { state.cursor = state.next; refresh(true); });
    buttons.append(first, next); pager.append(buttons); box.append(pager); return box;
  }
  function repositoryLink(repository) {
    const cell = element("div"), link = element("a", "repository-link", repository.name + (repository.name_truncated ? "…" : ""));
    link.href = `#repo/${repository.id}`; link.dataset.focusKey = `repo-${repository.id}`; link.title = repository.name;
    const uuid = element("span", "repository-uuid mono", repository.svn_uuid); uuid.title = repository.svn_uuid; cell.append(link, uuid); return cell;
  }
  function eventPanel(items, title = "近期事件") {
    const box = panel(title, "当前实例 · 最多 200 条"), body = element("div", "panel-body"), list = element("ul", "events");
    const names = { command_denied: "命令被权限规则拒绝", command_failed: "命令执行失败", query_failed: "数据库查询失败", ok: "数据库采样成功", database_unavailable: "数据库暂时不可达", writer_unavailable: "写主状态无法确认", configuration_unavailable: "仓库路由或配置不可用", reader_role_not_restricted: "监控读取角色的权限过大", heartbeat_unavailable: "遥测发布失败", schema_or_primary_invalid: "数据库结构或主库状态不符合要求", snapshot_capacity: "状态样本超过容量上限", token_capacity: "有效令牌超过容量上限", http_4xx: "监控请求被拒绝", http_5xx: "监控请求暂时不可用", http_cancelled: "监控响应已取消", business_ready: "仓库服务就绪" };
    for (const event of items || []) {
      const li = element("li"), text = element("div"), line = element("div", "event-line");
      const warning = /denied|http_4xx|capacity|reader_role_not_restricted/.test(event.code), bad = !warning && /failed|unavailable|http_5xx|invalid/.test(event.code), good = ["ok", "business_ready"].includes(event.code);
      const marker = icon(bad ? "x-circle" : warning ? "warning" : good ? "check-circle" : "info"); marker.classList.add(bad ? "danger" : warning ? "warning" : good ? "good" : "neutral");
      line.append(element("span", "", names[event.code] || "服务状态更新"), element("small", "", time(event.sampled_at)));
      text.append(line, element("span", "event-meta", `${event.source}${event.operation ? ` / ${event.operation}` : ""}`));
      if (event.request_id) text.append(element("span", "event-meta mono", event.request_id)); li.append(marker, text); list.append(li);
    }
    body.append(list.children.length ? list : empty("没有近期错误", "该窗口内未记录相关事件。")); box.append(body); return box;
  }
  function summaryTable(title, columns, collection, convert, emptyTitle) {
    const box = panel(title, `${collection?.items?.length || 0} 条 / 当前样本`), rows = collection?.items || [];
    box.append(rows.length ? table(columns, rows.map(convert), title) : empty(emptyTitle, "最近一次成功采样没有记录。"));
    if (collection?.truncated || collection?.next_cursor) box.append(element("div", "pager", "本区仅展示有界摘要，部分记录未展示。")); return box;
  }
  function nodeValue(node, key) { return ["状态未知", "心跳失联", "已停止"].includes(nodeInfo(node).text) ? "未知" : number(node.metrics?.[key]); }
  function nodeDetails(node) {
    const label = `${node.node_id} / ${node.instance_id.slice(0, 8)}`;
    if (node.component !== "dav") return mono(label);
    const box = element("details", "node-details"), summary = element("summary", "mono"); summary.append(icon("caret-down"), element("span", "", label)); summary.title = node.instance_id;
    box.dataset.chart = `node-${node.instance_id}`; box.append(summary); const body = element("div", "dav-detail-body"); body.setAttribute("role", "group"); body.setAttribute("aria-label", `${label} · DAV 工作进程详情`);
    if (["状态未知", "心跳失联", "已停止"].includes(nodeInfo(node).text)) { body.append(element("p", "muted", "当前工作进程指标不可用。")); box.append(body); return box; }
    const info = element("div"); info.append(details([["HTTP 请求 / 失败", `${nodeValue(node, "requests")} / ${nodeValue(node, "request_errors")}`], ["正在处理请求", nodeValue(node, "requests_active")], ["SQL 调用 / 失败", `${nodeValue(node, "sql_calls")} / ${nodeValue(node, "sql_errors")}`], ["新修订 / 回执确认", `${nodeValue(node, "published_revisions")} / ${nodeValue(node, "receipt_confirmations")}`], ["遥测失败批次", nodeValue(node, "telemetry_failures")]])); body.append(info);
    const methods = Array.isArray(node.metrics?.methods) ? node.metrics.methods.slice(0, 17).filter(row => row && row.requests > 0) : [], max = Math.max(1, ...methods.map(row => row.requests));
    const methodBox = element("div"); methodBox.append(element("h3", "kpi-label", "HTTP 方法"));
    if (methods.length) methodBox.append(table(["方法", endColumn("完成"), endColumn("失败")], methods.map(row => { const name = element("div", "mono", String(row.method || "未知").slice(0, 32)); name.append(meter(row.requests, max)); return [name, number(row.requests), number(row.errors)]; }), "DAV 方法计数"));
    else methodBox.append(empty("尚无 HTTP 请求", "完成请求后显示。")); body.append(methodBox);
    const errors = Array.isArray(node.metrics?.recent_errors) ? node.metrics.recent_errors.slice(-5).filter(row => row && typeof row === "object") : [], errorBox = element("div"); errorBox.append(element("h3", "kpi-label", "近期错误"));
    if (errors.length) errorBox.append(table(["时间", endColumn("HTTP 状态"), "请求 ID"], errors.map(row => { const at = Number(row.at_unix_microseconds) / 1000; return [Number.isFinite(at) && Math.abs(at) <= 8640000000000000 ? time(at) : "未知", row.status === 0 ? "连接取消" : number(row.status), mono(String(row.request_id || "未知").slice(0, 32))]; }), "DAV 近期错误"));
    else errorBox.append(empty("没有近期错误", "最多保留 5 条工作进程错误。")); body.append(errorBox); box.append(body); return box;
  }
  function revisionTimeline(collection) {
    const box = panel("近期修订", `${collection?.items?.length || 0} 条 / 当前样本`), body = element("div", "panel-body"), list = element("ol", "events revision-timeline");
    for (const row of collection?.items || []) { const li = element("li"), text = element("div"); text.append(element("span", "mono", `r${row.rev}`), element("span", "event-meta", time(row.committed_at))); li.append(icon("git-branch"), text); list.append(li); }
    body.append(list.children.length ? list : empty("没有修订记录", "最近一次成功采样没有记录。")); box.append(body); return box;
  }
  function render() {
    const o = state.overview, p = o.process, d = o.database, root = $("content");
    const focused = root.contains(document.activeElement) || searchForm.contains(document.activeElement) ? document.activeElement : null, searchFocused = focused && searchForm.contains(focused);
    const focusedPage = focused?.dataset.page, focusedKey = focused?.dataset.focusKey, focusedChart = focused?.tagName === "SUMMARY" ? focused.parentElement.dataset.chart : null;
    const expanded = [...root.querySelectorAll("details[open]")].map(e => e.dataset.chart);
    const scrollKey = e => `${e.closest(".dav-detail-body")?.id || ""}|${e.dataset.scrollKey}`;
    const scrolls = new Map([...root.querySelectorAll(".table-wrap")].map(e => [scrollKey(e), [e.scrollLeft, e.scrollTop]]));
    state.chartObservers.forEach(observer => observer.disconnect()); state.chartObservers = [];
    root.before(searchForm); root.replaceChildren(); root.removeAttribute("aria-busy"); state.renderedEpoch = state.epoch;
    const databaseFresh = !state.updateFailed && !state.ageExpired && o.last_attempt_ok && o.freshness === "fresh", stale = !databaseFresh;
    const stats = element("div", "stats"), latest = state.trends?.items?.at(-1), rows = state.rows?.items || [];
    if (state.view === "overview") {
      const latencyTone = p.command_p95_overflow ? "danger" : p.command_p95_upper_bound_ms == null ? "neutral" : p.command_p95_upper_bound_ms <= limits.latencyGood ? "good" : "warning";
      stats.append(kpi("请求速率", latest?.commands_per_second == null ? null : number(latest.commands_per_second), "当前实例 · 最近采样区间", { unit: "/s", field: "commands_per_second", series: 1, icon: "pulse" }),
        kpi("失败率", latest?.error_percent == null ? null : number(latest.error_percent), "没有完成命令时为未知", { unit: "%", field: "error_percent", series: 2, icon: "warning", tone: latest?.error_percent >= limits.errorBad ? "danger" : latest?.error_percent > limits.errorWarn ? "warning" : "neutral" }),
        kpi("命令 P95 上界", p.command_p95_overflow ? "> 2000" : p.command_p95_upper_bound_ms == null ? null : `≤ ${number(p.command_p95_upper_bound_ms)}`, "直方图上界 · 刻线 500 / 1000 ms", { unit: "ms", tone: latencyTone, icon: "clock", meter: [p.command_p95_overflow ? limits.latencyMax : p.command_p95_upper_bound_ms, limits.latencyMax, { tone: latencyTone, ticks: [limits.latencyGood, limits.latencyTick] }] }),
        kpi("已观测在线节点", databaseFresh && d?.nodes_online != null ? `${number(d.nodes_online)} / ${number(d.nodes_total)}` : null, d?.nodes_total > 40 ? "数据库心跳样本 · 容量比例" : "数据库心跳样本 · 每节点一格", { stale, tone: !databaseFresh ? "neutral" : d?.nodes_online === d?.nodes_total ? "good" : "warning", icon: "hard-drives", meter: [databaseFresh ? d?.nodes_online : null, d?.nodes_total, { segments: true }] }));
    } else if (state.view === "nodes") {
      const known = !stale && state.rows.last_attempt_ok && state.rows.freshness === "fresh", ready = rows.filter(n => nodeInfo(n).text === "就绪").length, online = rows.filter(n => ["就绪", "未就绪", "在线 / 就绪未知"].includes(nodeInfo(n).text)).length, lost = rows.filter(n => nodeInfo(n).text === "心跳失联").length;
      const generations = rows.filter(n => !["状态未知", "心跳失联", "已停止"].includes(nodeInfo(n).text)).map(n => n.metrics?.configuration_generation).filter(Number.isFinite);
      stats.append(kpi("在线 / 总数", known ? `${online} / ${rows.length}` : null, "当前页 · 数据库心跳样本", { stale, icon: "hard-drives", meter: [known ? online : null, rows.length, { segments: true }] }), kpi("就绪节点", known ? ready : null, "当前页 · 已初始化并确认就绪", { stale, tone: known && ready > 0 ? "good" : "neutral", icon: "check-circle" }), kpi("心跳失联", known ? lost : null, "当前页 · 超过 15 秒未更新", { stale, tone: lost > 0 ? "warning" : "neutral", icon: "wifi-slash" }), kpi("最大配置代数", known && generations.length ? number(Math.max(...generations)) : null, "当前页 · 可用工作进程的观测值", { stale, icon: "git-branch" }));
    } else if (state.view === "detail") {
      const r = state.rows.repository;
      stats.append(kpi("最新修订", `r${r.head_rev}`, "数据库仓库历史", { stale, icon: "git-branch" }), kpi("配置代数", number(r.generation), `原生 SVN：${r.svn_authz_mode || "enforce"}`, { stale }), kpi("活动事务", number(r.active_transactions), "未完成的 SVN 事务", { stale, tone: r.active_transactions ? "warning" : "neutral" }), kpi("业务锁", number(r.lock_count), "有效 SVN 锁", { stale, icon: "lock-simple" }));
    } else if (state.view === "database") {
      const scale = d?.database_bytes >= 1073741824 ? 1073741824 : 1048576, leaseRatio = p.connection_limit ? p.connections_leased / p.connection_limit : null, tone = leaseRatio >= 1 ? "danger" : leaseRatio >= limits.leaseWarn ? "warning" : "neutral";
      stats.append(kpi("数据库容量", d?.database_bytes == null ? null : number(d.database_bytes / scale), "整个数据库，包含遥测", { stale, unit: scale === 1073741824 ? "GiB" : "MiB", icon: "database" }), kpi("业务连接租约", `${number(p.connections_leased)} / ${number(p.connection_limit)}`, "本实例，采样连接另计", { tone, meter: [p.connections_leased, p.connection_limit, { tone }] }), kpi("控制连接等待", number(p.control_waiters), "本实例 · 等待连接互斥锁", { tone: p.control_waiters ? "warning" : "neutral" }), kpi("采样耗时", number(o.sampler_milliseconds), "独立连接与状态查询", { stale, unit: "ms", tone: o.sampler_milliseconds > 1000 ? "warning" : "neutral", icon: "clock" }));
    } else {
      const sessionLimit = Number.isSafeInteger(p.sessions_limit) && p.sessions_limit > 0 ? p.sessions_limit : null;
      const sessionRatio = sessionLimit == null || p.sessions_active == null ? null : p.sessions_active / sessionLimit;
      const sessionTone = sessionRatio == null ? "neutral" : sessionRatio >= 1 ? "danger" : sessionRatio >= limits.sessionWarn ? "warning" : "neutral";
      stats.append(kpi("完成的命令", number(p.commands), "当前实例 · SVN 顶层命令", { icon: "pulse" }), kpi("当前会话", number(p.sessions_active), `当前实例 · 上限 ${number(sessionLimit)}`, { tone: sessionTone, meter: sessionLimit == null ? null : [p.sessions_active, sessionLimit, { tone: sessionTone }] }), kpi("新发布修订", number(p.published_revisions), `回执确认 ${number(p.receipt_confirmations)} 次`, { icon: "git-branch" }), kpi("活动事务", number(d?.active_transactions), "数据库样本 · 全部仓库", { stale }));
    }
    root.append(stats);
    if (state.view === "overview") {
      const columns = element("div", "columns"), info = panel("本机状态", "进程实时指标"), body = element("div", "panel-body");
      const cpu = element("div", "", p.cpu_percent_of_machine == null ? "未知" : `${number(p.cpu_percent_of_machine)}%`); cpu.append(meter(p.cpu_percent_of_machine, 100));
      body.append(details([["节点", o.node_id], ["版本", p.version], ["启动时间", time(p.started_at)], ["启动实例", o.instance_id, true], ["CPU（整机比例）", cpu], ["工作集 / 私有提交", p.working_set_bytes == null ? "未知" : `${number(p.working_set_bytes / 1048576)} / ${number(p.private_bytes / 1048576)} MiB`], ["完成命令 / 失败", `${number(p.commands)} / ${number(p.errors)}`], ["协议接收 / 发送", `${number(p.bytes_received / 1048576)} / ${number(p.bytes_sent / 1048576)} MiB`]])); info.append(body); columns.append(chart(), info); root.append(columns);
      const trends = element("div", "columns equal"); trends.append(chart("失败率", "error_percent", "%", 2), chart("平均命令延迟", "mean_latency_ms", "毫秒", 3)); root.append(trends);
      const sorted = [...rows].sort((a, b) => nodeInfo(a).rank - nodeInfo(b).rank); root.append(paged("已观测节点", ["状态", "节点", "入口", "实例", "心跳"], sorted.map(n => [nodeStatus(n), n.node_id, n.component === "svn" ? "SVN" : "DAV", mono(n.instance_id, true), timestamp(n.last_seen_at)]), "尚未观测到节点"));
    } else if (state.view === "nodes") {
      const sorted = [...rows].sort((a, b) => nodeInfo(a).rank - nodeInfo(b).rank);
      root.append(paged("节点列表", ["状态", "节点 / 实例", "入口", "版本", endColumn("连接租约"), endColumn("最大配置代数"), "最后心跳"], sorted.map(n => [nodeStatus(n), nodeDetails(n), n.component.toUpperCase(), element("span", "nowrap", n.version), nodeValue(n, "connections"), nodeValue(n, "configuration_generation"), timestamp(n.last_seen_at)]), "尚未观测到节点"));
    } else if (state.view === "repositories") root.append(paged(state.search ? "仓库搜索结果" : "仓库列表", ["名称 / UUID", "状态", endColumn("HEAD / 配置"), "最近提交", endColumn("事务 / 锁")], rows.map(r => [repositoryLink(r), badge(r.enabled ? "启用" : "禁用", r.enabled ? "good" : "neutral"), `r${r.head_rev} / ${r.generation}`, time(r.last_commit_at), element("span", r.active_transactions || r.lock_count ? "warning mono" : "mono", `${number(r.active_transactions)} / ${number(r.lock_count)}`)]), state.search ? "没有匹配的仓库" : "还没有仓库"));
    else if (state.view === "detail") {
      const r = state.rows.repository, back = element("a", "back-link"); back.append(icon("arrow-left"), element("span", "", "返回仓库列表")); back.href = "#repositories"; back.dataset.focusKey = "back-repositories";
      const heading = element("div", "repository-heading"); heading.append(element("h2", "", r.name), badge(r.enabled ? "启用" : "禁用", r.enabled ? "good" : "neutral"), mono(r.svn_uuid)); root.append(back, heading);
      const info = panel("仓库信息", "数据库目录"), body = element("div", "panel-body"); body.append(details([["仓库 ID", r.id, true], ["公开 UUID", r.svn_uuid, true], ["启用状态", r.enabled ? "启用" : "禁用"], ["最近提交", time(r.last_commit_at)]])); info.append(body);
      const columns = element("div", "columns"), history = element("div", "stack"), facts = element("div", "stack");
      history.append(revisionTimeline(state.rows.recent_revisions), summaryTable("活动事务", ["事务 ID", endColumn("基础修订"), "创建时间"], state.rows.transactions, row => [mono(row.transaction_id + (row.id_truncated ? "…" : "")), `r${row.base_rev}`, time(row.created_at)], "没有活动事务"), summaryTable("业务锁", ["路径", "所有者", "创建时间"], state.rows.locks, row => [row.path + (row.path_truncated ? "…" : ""), row.owner + (row.owner_truncated ? "…" : ""), time(row.creation_date == null ? null : row.creation_date / 1000)], "没有业务锁"));
      facts.append(info, eventPanel(state.rows.recent_errors?.items, "本机近期相关错误")); columns.append(history, facts); root.append(columns);
    } else if (state.view === "database") {
      const box = panel("连接与查询", "数据库样本 / 本机累计"), body = element("div", "panel-body");
      body.append(details([["数据库状态", databaseFresh ? "可用" : "不可用 / 过期"], ["PostgreSQL 版本", d?.version == null ? "未知" : `${Math.floor(d.version / 10000)}.${d.version % 10000}`], ["写主状态", !databaseFresh || d?.writable == null ? "未知" : d.writable ? "可写主库" : "未确认可写"], ["受限只读采样", d?.reader_read_only && d?.reader_restricted ? "已启用" : "未使用独立受限角色"], ["同角色集群连接 / 锁等待", !databaseFresh ? "未知" : `${number(d?.application_connections_same_role)} / ${number(d?.backend_lock_waits_same_role)}`], ["SQL 调用 / 失败", `${number(p.sql_calls)} / ${number(p.sql_errors)}`], ["正在执行 SQL", number(p.sql_active)], ["SQL 总耗时", `${number(p.sql_milliseconds)} ms`], ["打开写流", number(p.writers_active)], ["采样失败 / 丢弃批次", `${number(o.sample_failures)} / ${number(o.dropped_telemetry_batches)}`]])); box.append(body);
      const categories = panel("SQLSTATE 分类", "按完成计数 · 条长为对数比例"), entries = Object.entries(p.sqlstate_classes || {}), maximum = Math.max(1, ...entries.map(([, n]) => n));
      categories.append(table(["类别 / 对数比例", endColumn("次数")], entries.map(([key, value]) => { const cell = element("div", "", key === "00" ? "00 · 成功" : key === "other" ? "其他错误" : `${key} · 错误类别`); cell.append(meter(Math.log1p(value), Math.log1p(maximum), { tone: key === "00" ? "good" : "danger" })); return [cell, number(value)]; }), "SQLSTATE 分类"));
      const columns = element("div", "columns equal"); columns.append(box, categories); root.append(columns, paged("活动事务", ["仓库 ID", "事务 ID", endColumn("基础修订"), "创建时间"], rows.map(tx => [mono(tx.repository_id), mono(tx.transaction_id + (tx.id_truncated ? "…" : "")), `r${tx.base_rev}`, time(tx.created_at)]), "没有活动事务"));
    } else {
      const columns = element("div", "columns"), used = rows.filter(r => r.success || r.denied || r.error), box = panel("命令结果", "当前实例 · SVN 入口 · 成功 / 拒绝 / 失败");
      box.append(used.length ? table(["操作", endColumn("成功"), endColumn("权限拒绝"), endColumn("其他失败"), "结果分布"], used.map(r => [r.operation, number(r.success), number(r.denied), number(r.error), resultBar([r.success, r.denied, r.error])]), "命令结果") : empty("尚未完成业务命令", "客户端访问仓库后显示计数。"));
      const monitor = panel("监控入口", "与业务命令分别计数"), body = element("div", "panel-body"); body.append(details([["已完成 HTTP 请求", number(p.monitor_requests)], ["错误响应 / 取消", number(p.monitor_errors)], ["正在处理", number(p.monitor_active)]])); monitor.append(body); columns.append(box, monitor); root.append(columns, eventPanel(state.events?.items));
    }
    for (const key of expanded) { const item = root.querySelector(`details[data-chart="${key}"]`); if (item) { item.open = true; if (item.pgvnExpandedRow) item.pgvnExpandedRow.hidden = false; } }
    for (const e of root.querySelectorAll(".table-wrap")) { const key = scrollKey(e); e.dataset.focusKey = `table-${encodeURIComponent(key)}`; const at = scrolls.get(key); if (at) { e.scrollLeft = at[0]; e.scrollTop = at[1]; } }
    if (focused) { const target = searchFocused ? focused : focusedPage ? root.querySelector(`[data-page="${focusedPage}"]:not(:disabled)`) || root.querySelector("[data-page]:not(:disabled)") : focusedKey ? root.querySelector(`[data-focus-key="${focusedKey}"]`) : focusedChart ? root.querySelector(`details[data-chart="${focusedChart}"] summary`) : null; (target || $("main")).focus({ preventScroll: true }); }
    state.nodeStatusKey = nodeStatusKey(); healthStrip();
  }
  function navigate() {
    const focusMain = $("content").contains(document.activeElement);
    const view = location.hash.slice(1), detail = /^repo\/([0-9a-f-]{36})$/i.exec(view);
    state.view = detail ? "detail" : Object.hasOwn(labels, view) && view !== "detail" ? view : "overview";
    state.repository = detail ? detail[1].toLowerCase() : ""; state.cursor = ""; state.epoch++;
    $("search-form").hidden = !state.token || state.view !== "repositories";
    $("breadcrumb").textContent = labels[state.view]; $("page-title").textContent = titles[state.view];
    document.querySelectorAll("[data-view]").forEach(a => { if (a.dataset.view === (state.view === "detail" ? "repositories" : state.view)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (state.token) { $("content").before(searchForm); state.chartObservers.forEach(observer => observer.disconnect()); state.chartObservers = []; $("content").replaceChildren(skeleton()); $("content").setAttribute("aria-busy", "true"); status("正在读取状态。"); if (focusMain) $("main").focus({ preventScroll: true }); refresh(true); }
  }
  $("token-form").addEventListener("submit", event => { event.preventDefault(); if (state.busy) return; state.token = $("token").value.trim(); $("token").value = ""; $("token-error").hidden = true; $("token").removeAttribute("aria-invalid"); state.cursor = ""; status("正在验证监控访问权限…"); refresh(); });
  $("health-retry").addEventListener("click", () => refresh(true));
  $("theme").addEventListener("click", () => { state.theme = (state.theme === "dark" || (!state.theme && scheme.matches)) ? "light" : "dark"; document.documentElement.dataset.theme = state.theme; syncTheme(); });
  scheme.addEventListener("change", () => { if (!state.theme) syncTheme(); });
  syncTheme(); healthStrip();
  $("refresh").addEventListener("click", () => refresh(true));
  $("logout").addEventListener("click", () => disconnect("已断开监控连接。"));
  $("pause").addEventListener("click", () => { state.paused = !state.paused; buttonIcon("pause", state.paused ? "play" : "pause", state.paused ? "继续刷新" : "暂停刷新"); healthStrip(); $("pause").setAttribute("aria-pressed", String(state.paused)); if (state.paused) { clearTimeout(state.timer); status("自动刷新已暂停，可手动刷新。"); } else refresh(); });
  $("search-form").addEventListener("submit", event => { event.preventDefault(); state.search = $("repository-search").value.trim(); state.cursor = ""; state.epoch++; refresh(true); });
  $("clear-search").addEventListener("click", () => { $("repository-search").value = ""; state.search = ""; state.cursor = ""; state.epoch++; refresh(true); });
  $("refresh-interval").addEventListener("change", () => { const seconds = Number($("refresh-interval").value); state.refreshSeconds = [5, 10, 30, 60].includes(seconds) ? seconds : 5; schedule(); if (state.token && !state.paused) status(`已设置每 ${state.refreshSeconds} 秒刷新。`); });
  document.querySelector(".skip").addEventListener("click", event => { event.preventDefault(); $("main").focus({ preventScroll: true }); });
  window.addEventListener("hashchange", navigate); document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.paused) refresh(); else schedule(); });
  window.addEventListener("pagehide", () => disconnect());
  fetch("/health/live", { cache: "no-store" }).then(r => { $("local-health").textContent = r.ok ? "本机监控在线" : "本机监控暂时不可用"; if (!state.token) status(r.ok ? "本机监控在线。连接后查看运行详情。" : "本机监控暂时不可用。", r.ok ? "" : "warning"); }).catch(() => { $("local-health").textContent = "无法连接本机监控"; if (!state.token) status("无法连接本机监控。", "danger"); }); navigate();
})();
