"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const labels = { overview: "概览", nodes: "节点", repositories: "仓库", database: "数据库", requests: "请求与错误", detail: "仓库详情" };
  const titles = { overview: "服务概览", nodes: "节点状态", repositories: "仓库状态", database: "数据库状态", requests: "请求与错误", detail: "仓库详情" };
  const state = { token: "", view: "overview", busy: false, paused: false, timer: null, cursor: "", next: null, overview: null, rows: null, trends: null, events: null, repository: "", search: "", epoch: 0, refreshSeconds: 5, renderedEpoch: -1 };
  const number = value => value == null ? "未知" : new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value);
  const time = value => value != null && value !== "" && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "未知";
  const element = (tag, className, text) => { const e = document.createElement(tag); if (className) e.className = className; if (text != null) e.textContent = text; return e; };
  function status(text, tone = "") { const node = $("connection-status"); if (node.textContent !== text) node.textContent = text; node.className = `connection-status ${tone}`; }
  function badge(text, tone) { return element("span", `badge ${tone}`, text); }
  function nodeStatus(node) {
    const sample = state.rows;
    if (!sample || !sample.last_attempt_ok || sample.freshness !== "fresh" || !Number.isFinite(node.age_seconds) || node.age_seconds < 0) return badge("状态未知", "neutral");
    if (node.metrics?.stopped === true) return badge("已停止", "neutral");
    if (node.age_seconds + (sample.age_seconds || 0) > 15) return badge("心跳失联", "warning");
    if (node.metrics?.ready === false) return badge("未就绪", "warning");
    return badge(node.metrics?.ready === true ? "就绪" : "在线 / 就绪未知", node.metrics?.ready === true ? "good" : "neutral");
  }
  function stat(label, value, note) { const e = element("article", "stat"); e.append(element("span", "stat-label", label), element("span", "stat-value", value), element("span", "stat-note", note)); return e; }
  function panel(title, subtitle) { const p = element("section", "panel"); const heading = element("div", "panel-heading"); heading.append(element("h2", "", title)); if (subtitle) heading.append(element("small", "", subtitle)); p.append(heading); return p; }
  function details(items) { const dl = element("dl", "detail-list"); for (const [title, value, mono] of items) dl.append(element("dt", "", title), element("dd", mono ? "mono" : "", value)); return dl; }
  function empty(title, text) { const box = element("div", "empty"); box.append(element("strong", "", title), element("span", "", text)); return box; }
  function table(columns, rows) {
    const wrap = element("div", "table-wrap"), t = element("table"), head = element("thead"), tr = element("tr");
    for (const column of columns) { const th = element("th", "", column); th.scope = "col"; tr.append(th); } head.append(tr); t.append(head);
    const body = element("tbody"); for (const row of rows) { const r = element("tr"); for (const cell of row) { const td = element("td"); if (cell instanceof Node) td.append(cell); else td.textContent = cell == null ? "未知" : String(cell); r.append(td); } body.append(r); }
    t.append(body); wrap.append(t); return wrap;
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
    state.token = ""; clearTimeout(state.timer); state.overview = null; state.rows = null; state.trends = null; state.events = null; state.next = null;
    $("content").hidden = true; $("content").replaceChildren(); $("login").hidden = false; $("logout").hidden = true; $("search-form").hidden = true; state.epoch++;
    $("readiness").textContent = "未连接"; $("readiness").className = "badge neutral"; if (message) status(message, "warning"); $("token").focus({ preventScroll: true });
  }
  async function refresh(force = false) {
    clearTimeout(state.timer); if (!state.token || state.busy) return;
    state.busy = true; $("refresh").setAttribute("aria-disabled", "true"); $("refresh").setAttribute("aria-busy", "true");
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
      Object.assign(state, { overview, rows, trends, events, next: rows.next_cursor });
      const firstConnection = !$("login").hidden;
      $("login").hidden = true; $("content").hidden = false; $("logout").hidden = false; $("search-form").hidden = view !== "repositories";
      if (firstConnection) $("main").focus({ preventScroll: true });
      const stale = overview.freshness !== "fresh" || !overview.last_attempt_ok;
      const ready = overview.ready;
      $("readiness").textContent = ready ? "服务就绪" : "尚未就绪"; $("readiness").className = `badge ${ready ? "good" : "warning"}`;
      $("subtitle").textContent = `${overview.node_id} · 实例 ${overview.instance_id.slice(0, 8)}`;
      $("sample-meta").textContent = `采样于 ${time(overview.sampled_at)} · ${overview.age_seconds == null ? "尚无样本" : `${number(overview.age_seconds)} 秒前`}`;
      status(stale ? "数据库状态暂时无法更新。保留上次成功样本，本机进程指标仍可查看。" : state.paused ? "自动刷新已暂停，可手动刷新。" : ready ? `服务运行正常。每 ${state.refreshSeconds} 秒自动刷新，只读访问。` : "监控已连接，仓库服务尚未就绪。", stale ? "warning" : "");
      render();
    } catch (error) {
      if (error.auth) { disconnect(error.message); $("token-error").textContent = error.message; $("token-error").hidden = false; }
      else if (error.cursor) { state.cursor = ""; status(error.message); setTimeout(refresh, 0); }
      else { status(error.name === "AbortError" ? "连接超时。当前页面保留上次读取结果。" : error.message, "danger"); if (state.renderedEpoch !== epoch) $("content").replaceChildren(empty("暂时无法读取状态", "请使用刷新按钮重试。")); }
    } finally { state.busy = false; $("refresh").removeAttribute("aria-disabled"); $("refresh").removeAttribute("aria-busy"); if (epoch !== state.epoch && state.token) setTimeout(() => refresh(true), 0); else schedule(); }
  }
  function chart(title = "命令速率", field = "commands_per_second", unit = "次 / 秒") {
    const box = panel(title, `最近 15 分钟 · ${unit}`), points = state.trends?.items || [];
    const values = points.map(p => p[field]).filter(v => Number.isFinite(v));
    if (values.length < 2) { box.append(empty("正在收集趋势", "足够样本后显示；无完成命令时，失败率和延迟为未知。")); return box; }
    const wrap = element("div", "chart"), ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"); svg.setAttribute("viewBox", "0 0 650 210"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", `${title}趋势；详细数值见下方数据表`);
    const make = (tag, attrs, text) => { const e = document.createElementNS(ns, tag); for (const [key, value] of Object.entries(attrs)) e.setAttribute(key, value); if (text != null) e.textContent = text; svg.append(e); return e; };
    const max = Math.max(0.1, Math.ceil(Math.max(...values) * 10) / 10), start = Date.parse(points[0].sampled_at), end = Date.parse(points.at(-1).sampled_at);
    for (let i = 0; i <= 3; i++) { const y = 18 + i * 50; make("line", { x1: 45, y1: y, x2: 630, y2: y, class: "grid" }); make("text", { x: 35, y: y + 4, "text-anchor": "end" }, new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(max * (1 - i / 3))); }
    let segment = [];
    const flush = () => { if (segment.length > 1) make("polyline", { points: segment.map(p => p.join(",")).join(" "), class: "line" }); else if (segment.length) make("circle", { cx: segment[0][0], cy: segment[0][1], r: 2, class: "point" }); segment = []; };
    for (const point of points) { if (!Number.isFinite(point[field])) { flush(); continue; } segment.push([45 + (Date.parse(point.sampled_at) - start) / (end - start || 1) * 585, 168 - point[field] / max * 150]); } flush();
    make("text", { x: 45, y: 195 }, new Date(start).toLocaleTimeString("zh-CN", { hour12: false })); make("text", { x: 630, y: 195, "text-anchor": "end" }, new Date(end).toLocaleTimeString("zh-CN", { hour12: false }));
    wrap.append(svg); box.append(wrap);
    const raw = element("details", "chart-details"); raw.dataset.chart = field; raw.append(element("summary", "", "查看趋势数据"), table(["采样时间", title], points.map(p => [time(p.sampled_at), number(p[field])]))); box.append(raw); return box;
  }
  function paged(title, columns, rows, emptyTitle) {
    const box = panel(title, `${state.rows?.items?.length || 0} 条 / 当前页`);
    if (rows.length) box.append(table(columns, rows)); else box.append(empty(emptyTitle, state.rows?.freshness === "unknown" ? "尚未取得数据库样本。" : "最近一次采样没有符合条件的记录。"));
    const pager = element("div", "pager"), buttons = element("div", "pager-buttons");
    pager.append(element("span", "", state.rows?.truncated ? "样本达到 1000 条上限，部分记录未展示。" : "每页最多 50 条，来自后台状态样本。"));
    const first = element("button", "", "第一页"); first.dataset.page = "first"; first.disabled = !state.cursor; first.addEventListener("click", () => { state.cursor = ""; refresh(true); });
    const next = element("button", "", "下一页"); next.dataset.page = "next"; next.disabled = !state.next; next.addEventListener("click", () => { state.cursor = state.next; refresh(true); });
    buttons.append(first, next); pager.append(buttons); box.append(pager); return box;
  }
  function repositoryLink(repository) {
    const link = element("a", "repository-link", repository.name + (repository.name_truncated ? "…" : ""));
    link.href = `#repo/${repository.id}`; link.dataset.focusKey = `repo-${repository.id}`; return link;
  }
  function eventPanel(items, title = "近期事件") {
    const box = panel(title, "当前实例 · 最多 200 条"), body = element("div", "panel-body"), list = element("ul", "events");
    const names = { command_denied: "命令被权限规则拒绝", command_failed: "命令执行失败", query_failed: "数据库查询失败", ok: "数据库采样成功", database_unavailable: "数据库暂时不可达", writer_unavailable: "写主状态无法确认", configuration_unavailable: "仓库路由或配置不可用", reader_role_not_restricted: "监控读取角色的权限过大", heartbeat_unavailable: "遥测发布失败", schema_or_primary_invalid: "数据库结构或主库状态不符合要求", snapshot_capacity: "状态样本超过容量上限", token_capacity: "有效令牌超过容量上限", http_4xx: "监控请求被拒绝", http_5xx: "监控请求暂时不可用", http_cancelled: "监控响应已取消", business_ready: "仓库服务就绪" };
    for (const event of items || []) { const li = element("li"); li.append(element("span", "", names[event.code] || "服务状态更新"), element("small", "", `${event.source}${event.operation ? ` / ${event.operation}` : ""} · ${time(event.sampled_at)}${event.request_id ? ` · ${event.request_id}` : ""}`)); list.append(li); }
    if (list.children.length) body.append(list); else body.append(empty("没有近期错误", "该窗口内未记录相关事件。")); box.append(body); return box;
  }
  function summaryTable(title, columns, collection, convert, emptyTitle) {
    const box = panel(title, `${collection?.items?.length || 0} 条 / 当前样本`), rows = collection?.items || [];
    box.append(rows.length ? table(columns, rows.map(convert)) : empty(emptyTitle, "最近一次成功采样没有记录。"));
    if (collection?.truncated || collection?.next_cursor) box.append(element("div", "pager", "本区仅展示有界摘要，部分记录未展示。"));
    return box;
  }
  function nodeValue(node, key) {
    const status = nodeStatus(node).textContent;
    return status === "状态未知" || status === "心跳失联" || status === "已停止" ? "未知" : number(node.metrics?.[key]);
  }
  function render() {
    const o = state.overview, p = o.process, d = o.database, root = $("content");
    const focused = root.contains(document.activeElement) ? document.activeElement : null;
    const focusedPage = focused?.dataset.page, focusedKey = focused?.dataset.focusKey;
    const focusedChart = focused?.tagName === "SUMMARY" ? focused.parentElement.dataset.chart : null;
    const expanded = [...root.querySelectorAll("details[open]")].map(e => e.dataset.chart);
    root.replaceChildren(); state.renderedEpoch = state.epoch;
    const databaseFresh = o.last_attempt_ok && o.freshness === "fresh";
    if (!databaseFresh) root.append(element("div", "alert", `数据库采样不可用或已过期，上次样本${o.age_seconds == null ? "尚未建立" : `距今 ${number(o.age_seconds)} 秒`}。数据库值保留最后样本，远端节点状态为未知。`));
    const stats = element("div", "stats"), latest = state.trends?.items?.at(-1);
    if (state.view === "overview") {
      stats.append(stat("请求速率", latest?.commands_per_second == null ? "未知" : `${number(latest.commands_per_second)} /s`, "当前实例 · 最近采样区间"), stat("失败率", latest?.error_percent == null ? "未知" : `${number(latest.error_percent)}%`, "没有完成命令时为未知"), stat("命令 P95 上界", p.command_p95_overflow ? "> 2000 ms" : p.command_p95_upper_bound_ms == null ? "未知" : `≤ ${number(p.command_p95_upper_bound_ms)} ms`, "当前实例 · 耗时直方图"), stat("已观测在线节点", databaseFresh && d?.nodes_online != null ? `${number(d.nodes_online)} / ${number(d.nodes_total)}` : "未知", "数据库心跳样本"));
    } else if (state.view === "detail") {
      const r = state.rows.repository;
      stats.append(stat("最新修订", `r${r.head_rev}`, "数据库仓库历史"), stat("配置代数", number(r.generation), "当前发布版本"), stat("活动事务", number(r.active_transactions), "未完成的 SVN 事务"), stat("业务锁", number(r.lock_count), "有效 SVN 锁"));
    } else if (state.view === "database") stats.append(stat("数据库容量", d?.database_bytes == null ? "未知" : `${number(d.database_bytes / 1048576)} MiB`, "整个数据库，包含遥测"), stat("业务连接租约", `${number(p.connections_leased)} / ${number(p.connection_limit)}`, "本实例，采样连接另计"), stat("控制连接等待", number(p.control_waiters), "本实例 · 等待连接互斥锁"), stat("采样耗时", `${number(o.sampler_milliseconds)} ms`, "独立连接与状态查询"));
    else stats.append(stat("完成的命令", number(p.commands), "当前实例 · SVN 顶层命令"), stat("当前会话", number(p.sessions_active), "当前实例 · 上限 16"), stat("新发布修订", number(p.published_revisions), `回执确认 ${number(p.receipt_confirmations)} 次`), stat("活动事务", number(d?.active_transactions), "数据库样本 · 全部仓库"));
    root.append(stats);
    const rows = state.rows?.items || [];
    if (state.view === "overview") {
      const columns = element("div", "columns"), info = panel("本机状态", "进程实时指标"), body = element("div", "panel-body");
      body.append(details([["节点", o.node_id], ["版本", p.version], ["启动时间", time(p.started_at)], ["启动实例", o.instance_id, true], ["CPU（整机比例）", p.cpu_percent_of_machine == null ? "未知" : `${number(p.cpu_percent_of_machine)}%`], ["工作集 / 私有提交", p.working_set_bytes == null ? "未知" : `${number(p.working_set_bytes / 1048576)} / ${number(p.private_bytes / 1048576)} MiB`], ["完成命令 / 失败", `${number(p.commands)} / ${number(p.errors)}`], ["协议接收 / 发送", `${number(p.bytes_received / 1048576)} / ${number(p.bytes_sent / 1048576)} MiB`]])); info.append(body); columns.append(chart(), info); root.append(columns);
      const trends = element("div", "columns equal"); trends.append(chart("失败率", "error_percent", "%"), chart("平均命令延迟", "mean_latency_ms", "毫秒")); root.append(trends);
      root.append(paged("已观测节点", ["节点", "入口", "实例", "心跳", "状态"], rows.map(n => [n.node_id, n.component === "svn" ? "SVN" : "DAV", n.instance_id.slice(0, 8), time(n.last_seen_at), nodeStatus(n)]), "尚未观测到节点"));
    } else if (state.view === "nodes") root.append(paged("节点列表", ["节点 / 实例", "入口", "版本", "连接租约", "最大配置代数", "最后心跳", "状态"], rows.map(n => [element("span", "mono", `${n.node_id} / ${n.instance_id.slice(0, 8)}`), n.component.toUpperCase(), n.version, nodeValue(n, "connections"), nodeValue(n, "configuration_generation"), time(n.last_seen_at), nodeStatus(n)]), "尚未观测到节点"));
    else if (state.view === "repositories") root.append(paged(state.search ? "仓库搜索结果" : "仓库列表", ["名称", "状态", "HEAD / 配置", "最近提交", "事务 / 锁", "UUID"], rows.map(r => [repositoryLink(r), badge(r.enabled ? "启用" : "禁用", r.enabled ? "good" : "neutral"), `r${r.head_rev} / ${r.generation}`, time(r.last_commit_at), `${number(r.active_transactions)} / ${number(r.lock_count)}`, element("span", "mono", r.svn_uuid)]), state.search ? "没有匹配的仓库" : "还没有仓库"));
    else if (state.view === "detail") {
      const r = state.rows.repository, back = element("a", "back-link", "返回仓库列表"); back.href = "#repositories"; back.dataset.focusKey = "back-repositories"; root.append(back);
      const info = panel(r.name, "仓库详情"), body = element("div", "panel-body"); body.append(details([["仓库 ID", r.id, true], ["公开 UUID", r.svn_uuid, true], ["启用状态", r.enabled ? "启用" : "禁用"], ["最近提交", time(r.last_commit_at)]])); info.append(body);
      const stack = element("div", "stack"); stack.append(info,
        summaryTable("近期修订", ["修订", "提交时间"], state.rows.recent_revisions, row => [`r${row.rev}`, time(row.committed_at)], "没有修订记录"),
        summaryTable("活动事务", ["事务 ID", "基础修订", "创建时间"], state.rows.transactions, row => [row.transaction_id + (row.id_truncated ? "…" : ""), `r${row.base_rev}`, time(row.created_at)], "没有活动事务"),
        summaryTable("业务锁", ["路径", "所有者", "创建时间"], state.rows.locks, row => [row.path + (row.path_truncated ? "…" : ""), row.owner + (row.owner_truncated ? "…" : ""), time(row.creation_date == null ? null : row.creation_date / 1000)], "没有业务锁"),
        eventPanel(state.rows.recent_errors?.items, "本机近期相关错误")); root.append(stack);
    } else if (state.view === "database") {
      const box = panel("连接与查询", "数据库样本 / 本机累计"), body = element("div", "panel-body");
      body.append(details([["数据库状态", databaseFresh ? "可用" : "不可用 / 过期"], ["PostgreSQL 版本", d?.version == null ? "未知" : `${Math.floor(d.version / 10000)}.${d.version % 10000}`], ["写主状态", !databaseFresh || d?.writable == null ? "未知" : d.writable ? "可写主库" : "未确认可写"], ["受限只读采样", d?.reader_read_only && d?.reader_restricted ? "已启用" : "未使用独立受限角色"], ["同角色集群连接 / 锁等待", !databaseFresh ? "未知" : `${number(d?.application_connections_same_role)} / ${number(d?.backend_lock_waits_same_role)}`], ["SQL 调用 / 失败", `${number(p.sql_calls)} / ${number(p.sql_errors)}`], ["正在执行 SQL", number(p.sql_active)], ["SQL 总耗时", `${number(p.sql_milliseconds)} ms`], ["打开写流", number(p.writers_active)], ["采样失败 / 丢弃批次", `${number(o.sample_failures)} / ${number(o.dropped_telemetry_batches)}`]])); box.append(body);
      const categories = panel("SQLSTATE 分类", "业务调用，按完成计数"); categories.append(table(["类别", "次数"], Object.entries(p.sqlstate_classes || {}).map(([key, value]) => [key === "00" ? "00 · 成功" : key === "other" ? "其他错误" : `${key} · 错误类别`, number(value)])));
      const stack = element("div", "stack"); stack.append(box, categories, paged("活动事务", ["仓库 ID", "事务 ID", "基础修订", "创建时间"], rows.map(tx => [tx.repository_id, tx.transaction_id + (tx.id_truncated ? "…" : ""), `r${tx.base_rev}`, time(tx.created_at)]), "没有活动事务")); root.append(stack);
    } else {
      const stack = element("div", "stack"), used = rows.filter(r => r.success || r.denied || r.error), box = panel("命令结果", "当前实例 · SVN 入口 · 按完成计数");
      box.append(used.length ? table(["操作", "成功", "权限拒绝", "其他失败"], used.map(r => [r.operation, number(r.success), number(r.denied), number(r.error)])) : empty("尚未完成业务命令", "客户端访问仓库后显示计数。"));
      const monitor = panel("监控入口", "与业务命令分别计数"), body = element("div", "panel-body"); body.append(details([["已完成 HTTP 请求", number(p.monitor_requests)], ["错误响应 / 取消", number(p.monitor_errors)], ["正在处理", number(p.monitor_active)]])); monitor.append(body);
      stack.append(box, monitor, eventPanel(state.events?.items)); root.append(stack);
    }
    for (const key of expanded) { const item = root.querySelector(`details[data-chart="${key}"]`); if (item) item.open = true; }
    if (focused) { const target = focusedPage ? root.querySelector(`[data-page="${focusedPage}"]:not(:disabled)`) || root.querySelector("[data-page]:not(:disabled)") : focusedKey ? root.querySelector(`[data-focus-key="${focusedKey}"]`) : focusedChart ? root.querySelector(`details[data-chart="${focusedChart}"] summary`) : null; (target || $("main")).focus({ preventScroll: true }); }
  }
  function navigate() {
    const focusMain = $("content").contains(document.activeElement);
    const view = location.hash.slice(1), detail = /^repo\/([0-9a-f-]{36})$/i.exec(view);
    state.view = detail ? "detail" : Object.hasOwn(labels, view) && view !== "detail" ? view : "overview";
    state.repository = detail ? detail[1].toLowerCase() : ""; state.cursor = ""; state.epoch++;
    $("search-form").hidden = !state.token || state.view !== "repositories";
    $("breadcrumb").textContent = labels[state.view]; $("page-title").textContent = titles[state.view];
    document.querySelectorAll("[data-view]").forEach(a => { if (a.dataset.view === (state.view === "detail" ? "repositories" : state.view)) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    if (state.token) { $("content").replaceChildren(empty("正在读取状态", "读取当前页面的最新样本。")); if (focusMain) $("main").focus({ preventScroll: true }); refresh(true); }
  }
  $("token-form").addEventListener("submit", event => { event.preventDefault(); if (state.busy) return; state.token = $("token").value.trim(); $("token").value = ""; $("token-error").hidden = true; state.cursor = ""; status("正在验证监控访问权限…"); refresh(); });
  $("refresh").addEventListener("click", () => refresh(true));
  $("logout").addEventListener("click", () => disconnect("已断开监控连接。"));
  $("pause").addEventListener("click", () => { state.paused = !state.paused; $("pause").textContent = state.paused ? "继续刷新" : "暂停刷新"; $("pause").setAttribute("aria-pressed", String(state.paused)); if (state.paused) { clearTimeout(state.timer); status("自动刷新已暂停，可手动刷新。"); } else refresh(); });
  $("search-form").addEventListener("submit", event => { event.preventDefault(); state.search = $("repository-search").value.trim(); state.cursor = ""; state.epoch++; refresh(true); });
  $("clear-search").addEventListener("click", () => { $("repository-search").value = ""; state.search = ""; state.cursor = ""; state.epoch++; refresh(true); });
  $("refresh-interval").addEventListener("change", () => { const seconds = Number($("refresh-interval").value); state.refreshSeconds = [5, 10, 30, 60].includes(seconds) ? seconds : 5; schedule(); if (state.token && !state.paused) status(`已设置每 ${state.refreshSeconds} 秒刷新。`); });
  document.querySelector(".skip").addEventListener("click", event => { event.preventDefault(); $("main").focus({ preventScroll: true }); });
  window.addEventListener("hashchange", navigate); document.addEventListener("visibilitychange", () => { if (!document.hidden && !state.paused) refresh(); else schedule(); });
  window.addEventListener("pagehide", () => { state.token = ""; clearTimeout(state.timer); });
  fetch("/health/live", { cache: "no-store" }).then(r => status(r.ok ? "本机监控在线。连接后查看运行详情。" : "本机监控暂时不可用。", r.ok ? "" : "warning")).catch(() => status("无法连接本机监控。", "danger")); navigate();
})();
