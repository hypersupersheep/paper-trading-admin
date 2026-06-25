"use strict";

// ---- 工具 ---------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };

function fmtMoney(v) {
  if (v == null) return "—";
  const n = Number(v);
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return n.toLocaleString("zh-CN", { maximumFractionDigits: 0 });
}
function fmtPct(v) { return v == null ? "—" : (v * 100).toFixed(2) + "%"; }
function signClass(v) { return v == null ? "flat" : v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return Math.round(s) + "s 前";
  if (s < 3600) return Math.round(s / 60) + "m 前";
  return Math.round(s / 3600) + "h 前";
}

// 带 token 的请求;写操作 401 时提示输入 token 并重试一次
async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = localStorage.getItem("admin_token");
  if (token) headers["X-Admin-Token"] = token;
  let resp = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (resp.status === 401) {
    const t = prompt("此操作需要 Admin Token:");
    if (t) {
      localStorage.setItem("admin_token", t);
      headers["X-Admin-Token"] = t;
      resp = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    }
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

// 内联 SVG sparkline
function sparkline(values) {
  if (!values || values.length < 2) return `<svg class="spark"></svg>`;
  const w = 240, h = 34, pad = 2;
  const min = Math.min(...values), max = Math.max(...values), range = max - min || 1;
  const step = (w - pad * 2) / (values.length - 1);
  const pts = values.map((v, i) => `${(pad + i * step).toFixed(1)},${(h - pad - ((v - min) / range) * (h - pad * 2)).toFixed(1)}`).join(" ");
  // A股口径:走高=红(up),走低=绿(down)。用 currentColor + 类着色(SVG 属性里 var() 不生效)
  const cls = values[values.length - 1] >= values[0] ? "up" : "down";
  return `<svg class="spark ${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}" /></svg>`;
}

// ---- 渲染:监控墙 -------------------------------------------------------
const STATUS_LABEL = { online: "", offline: "离线", degraded: "连接异常", missing: "节点上已删", unknown: "未知" };

// HTML 转义(描述是自由文本/AI 生成,防注入)
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function truncate(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n) + "…" : s; }
function fmtSize(b) {
  if (b == null) return "";
  return b >= 1048576 ? (b / 1048576).toFixed(1) + "MB" : Math.max(1, Math.round(b / 1024)) + "KB";
}

// 带正负号的格式化(ticker 用,正数加 +)
function fmtSigned(v, kind) {
  if (v == null) return "—";
  const body = kind === "pct" ? fmtPct(v) : fmtMoney(v);
  return (Number(v) > 0 ? "+" : "") + body;
}

// ---- 顶部滚动横条 -------------------------------------------------------
function tickerItem(label, value, cls) {
  return `<span class="tk"><b>${label}</b><span class="tk-val ${cls || ""}">${value}</span></span>`;
}

function buildTickerItems(d) {
  const t = d.totals || {};
  const online = (d.accounts || []).filter((a) => a.status === "online");
  const out = [];
  out.push(tickerItem("节点", `${t.node_online}/${t.node_count} 在线`));
  const owners = new Set(online.map((a) => a.owner).filter(Boolean));
  out.push(tickerItem("在线交易员", `${owners.size} 人`));
  out.push(tickerItem("在线账户", `${t.online}/${t.account_count}`));
  out.push(tickerItem("总净值", fmtMoney(t.equity)));
  out.push(tickerItem("总盈亏", fmtSigned(t.pnl), signClass(t.pnl)));

  if (online.length) {
    // 最佳交易员:按 owner 聚合收益率 = Σpnl / Σ(本金),本金 = 净值 - 盈亏
    const agg = {};
    for (const a of online) {
      const o = a.owner || a.name;
      const e = (agg[o] ||= { pnl: 0, base: 0 });
      e.pnl += a.pnl || 0;
      e.base += (a.equity || 0) - (a.pnl || 0);
    }
    let bo = null, br = -Infinity;
    for (const [o, e] of Object.entries(agg)) {
      const r = e.base > 0 ? e.pnl / e.base : 0;
      if (r > br) { br = r; bo = o; }
    }
    if (bo !== null) out.push(tickerItem("最佳交易员", `${bo} ${fmtSigned(br, "pct")}`, signClass(br)));

    // 最佳策略:单账户最高累计收益率
    const byPct = online.filter((a) => a.pnl_pct != null).sort((x, y) => y.pnl_pct - x.pnl_pct);
    if (byPct.length) {
      const a = byPct[0];
      out.push(tickerItem("最佳策略", `${a.name} ${fmtSigned(a.pnl_pct, "pct")}`, signClass(a.pnl_pct)));
    }
    // 本日最佳 / 本日最差:按当日盈亏
    const byDay = online.filter((a) => a.day_pnl != null).sort((x, y) => y.day_pnl - x.day_pnl);
    if (byDay.length) {
      const top = byDay[0];
      out.push(tickerItem("本日最佳", `${top.name} ${fmtSigned(top.day_pnl)}`, signClass(top.day_pnl)));
      const bot = byDay[byDay.length - 1];
      if (bot && bot !== top) out.push(tickerItem("本日最差", `${bot.name} ${fmtSigned(bot.day_pnl)}`, signClass(bot.day_pnl)));
    }
  }
  return out;
}

let _lastTicker = "";
function renderTicker(d) {
  const items = buildTickerItems(d).join("");
  if (items === _lastTicker) return;       // 内容没变就不重建,避免滚动动画被打断
  _lastTicker = items;
  $("#ticker-track").innerHTML = items + items;  // 两份拼接 → translateX(-50%) 无缝循环
}

function renderTotals(t) {
  $("#totals").innerHTML = `
    <div class="kv"><b class="${signClass(t.pnl)}">${fmtMoney(t.pnl)}</b><span>在线账户盈亏</span></div>
    <div class="kv"><b>${fmtMoney(t.equity)}</b><span>在线账户净值</span></div>
    <div class="kv"><b>${t.online}/${t.account_count}</b><span>在线账户</span></div>
    <div class="kv"><b>${t.node_online}/${t.node_count}</b><span>在线节点</span></div>`;
}

const STATUS_RANK = { online: 0, degraded: 1, missing: 2, unknown: 3, offline: 4 };

// 监控墙:按机器(节点)分组 —— 一台机器一个小标题,其下一排该机器的账户卡
function renderWall(accounts, nodes) {
  const wall = $("#wall");
  wall.innerHTML = "";
  if (!accounts.length) {
    wall.appendChild(el("div", "empty", "还没有已登记账户。节点接入并开户后,账户会自动登记上墙。"));
    return;
  }
  const nodeMap = {};
  for (const n of nodes || []) nodeMap[n.id] = n;
  // 按 node_id 归组
  const groups = {};
  for (const a of accounts) (groups[a.node_id] ||= []).push(a);
  // 机器排序:在线优先,再按机器名
  const nodeIds = Object.keys(groups).sort((x, y) => {
    const sx = (nodeMap[x] || {}).status, sy = (nodeMap[y] || {}).status;
    return (STATUS_RANK[sx] ?? 3) - (STATUS_RANK[sy] ?? 3)
      || (groups[x][0].node_name || "").localeCompare(groups[y][0].node_name || "");
  });

  for (const nid of nodeIds) {
    const accts = groups[nid].sort((a, b) => (a.name || "").localeCompare(b.name || ""));
    const node = nodeMap[nid] || {};
    const machineName = accts[0].node_name || nid;
    const nodeStatus = node.status || (accts.some((a) => a.status === "online") ? "online" : "offline");
    const group = el("section", "node-group");
    const head = el("div", "node-header");
    // 本席汇总盈亏(仅统计有数值的账户)
    const valid = accts.filter((a) => a.pnl != null && a.equity != null);
    let deskHtml = "";
    if (valid.length) {
      const dp = valid.reduce((s, a) => s + a.pnl, 0);
      const db = valid.reduce((s, a) => s + (a.equity - a.pnl), 0);
      const dr = db > 0 ? dp / db : null;
      deskHtml = `<span class="desk-pnl ${signClass(dp)}">${fmtSigned(dp)}${dr != null ? "&nbsp;·&nbsp;" + fmtSigned(dr, "pct") : ""}</span>`;
    }
    head.innerHTML = `<span class="dot ${nodeStatus}" aria-hidden="true"></span>
      <span class="mname">${machineName}</span>
      <span class="meta">${accts.length} 个账户${nodeStatus !== "online" ? " · " + (STATUS_LABEL[nodeStatus] || nodeStatus) : ""}</span>
      ${deskHtml}`;
    group.appendChild(head);
    const grid = el("div", "grid");
    for (const a of accts) grid.appendChild(accountCard(a));
    group.appendChild(grid);
    wall.appendChild(group);
  }
}

// 单张账户卡:账户名为主、交易员(owner)为辅
function accountCard(a) {
  const edge = a.pnl_pct == null ? "" : a.pnl_pct > 0 ? " up-edge" : a.pnl_pct < 0 ? " down-edge" : "";
  const card = el("div", "card" + (a.status === "offline" ? " offline" : "") + edge);
  const note = a.status !== "online"
    ? `<span class="stale">${STATUS_LABEL[a.status] || a.status}${a.last_ok_at ? " · " + ago(a.last_ok_at) : ""}</span>` : "";
  card.innerHTML = `
    <div class="head">
      <span class="dot ${a.status === "missing" ? "degraded" : a.status}" aria-hidden="true"></span>
      <span class="name">${a.name}</span>
      ${note}
    </div>
    ${a.owner && a.owner !== a.name ? `<div class="acct-sub">${a.owner}</div>` : ""}
    ${a.description ? `<div class="desc" title="${esc(a.description)}">${esc(truncate(a.description, 76))}</div>` : ""}
    <div class="metrics">
      <div class="metric lead"><span>总收益率</span><b class="${signClass(a.pnl_pct)}">${fmtSigned(a.pnl_pct, "pct")}</b></div>
      <div class="metric"><span>当日盈亏</span><b class="${signClass(a.day_pnl)}">${fmtSigned(a.day_pnl)}</b></div>
      <div class="metric"><span>净值</span><b>${fmtMoney(a.equity)}</b></div>
      <div class="metric"><span>仓位</span><b>${fmtPct(a.exposure)}</b></div>
    </div>
    ${sparkline(a.spark)}
    <div class="foot"><span>${a.owner || ""}</span><span>${a.position_count ?? "—"} 持仓</span></div>`;
  const open = () => openAccount(a.node_id, a.account_id, a.owner, a.name, a.description);
  card.setAttribute("role", "button");
  card.setAttribute("tabindex", "0");
  card.setAttribute("aria-label",
    `${a.owner || a.name} 账户 ${a.name},${a.status === "online" ? "在线" : STATUS_LABEL[a.status] || a.status},总收益率 ${fmtPct(a.pnl_pct)}`);
  card.onclick = open;
  card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  return card;
}

function renderLeaderboard(rows) {
  const lb = $("#leaderboard");
  lb.innerHTML = "";
  if (!rows.length) { lb.appendChild(el("li", "empty", "暂无在线数据")); return; }
  for (const r of rows) {
    const li = el("li");
    li.innerHTML = `<span class="lname">${r.name}<small>${r.owner && r.owner !== r.name ? " · " + r.owner : ""}</small></span>
      <span class="lpct ${signClass(r.pnl_pct)}">${fmtPct(r.pnl_pct)}</span>`;
    lb.appendChild(li);
  }
}

function renderAlerts(rows) {
  const box = $("#alerts");
  $("#alert-count").textContent = rows.length ? `(${rows.length} 未确认)` : "";
  box.innerHTML = "";
  if (!rows.length) { box.appendChild(el("li", "empty", "无未确认告警")); return; }
  for (const a of rows) {
    const li = el("li", a.severity);
    li.innerHTML = `<div class="amsg">${a.message}<div class="ats">${ago(a.ts)}</div></div>
      <button class="ack" type="button" data-id="${a.id}">确认</button>`;
    li.querySelector(".ack").onclick = async (e) => {
      e.stopPropagation();
      await api("POST", `/api/admin/alerts/${a.id}/ack`);
      refresh();
    };
    box.appendChild(li);
  }
}

// ---- 下钻弹窗 -----------------------------------------------------------
async function openAccount(nodeId, accountId, owner, name, description) {
  $("#modal-title").textContent = `${owner} · ${name}`;
  $("#modal-body").innerHTML = "加载中…";
  $("#modal").hidden = false;
  const aid = encodeURIComponent(accountId), nid = encodeURIComponent(nodeId);
  try {
    const [detail, trades, desc] = await Promise.all([
      api("GET", `/api/admin/nodes/${nid}`),
      api("GET", `/api/admin/nodes/${nid}/trades`),
      // 策略描述 + 文件清单(节点离线/旧版会失败,降级用卡片缓存的描述)
      api("GET", `/api/admin/nodes/${nid}/accounts/${aid}/description`).catch(() => null),
    ]);
    const descData = desc || (description ? { description, files: [] } : null);
    $("#modal-body").innerHTML = renderAccountDetail(accountId, nodeId, detail, trades.trades || [], descData);
    const nodeName = (detail.node && detail.node.name) || nodeId;
    $("#act-open-account").onclick = () => openAccountForm(nodeId, nodeName);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="msg err">${e.message}</div>`;
  }
}

function renderAccountDetail(accountId, nodeId, detail, allTrades, desc) {
  const accounts = (detail.summary && detail.summary.accounts) || [];
  const a = accounts.find((x) => x.id === accountId);
  let html = `<div class="form-actions" style="justify-content:flex-start">
      <button class="btn-primary" id="act-open-account">在该节点远程开户</button>
    </div>`;

  // 策略描述(文字 + 说明文件链接,文件经 Admin 代理透传)
  const files = (desc && desc.files) || [];
  if (desc && (desc.description || files.length)) {
    html += `<div class="section-title">策略描述</div>`;
    if (desc.description) html += `<div class="desc-full">${esc(desc.description)}</div>`;
    if (files.length) {
      const nid = encodeURIComponent(nodeId), aid = encodeURIComponent(accountId);
      html += `<div class="files">` + files.map((f) =>
        `<a class="file" href="/api/admin/nodes/${nid}/accounts/${aid}/files/${encodeURIComponent(f.id)}" target="_blank" rel="noopener">📄 ${esc(f.filename)}<small>${fmtSize(f.size)}</small></a>`
      ).join("") + `</div>`;
    }
  }

  if (a) {
    html += `<div class="section-title">净值 ${fmtMoney(a.equity)} · 收益 <span class="${signClass(a.pnl_pct)}">${fmtPct(a.pnl_pct)}</span> · 当日 <span class="${signClass(a.day_pnl)}">${fmtMoney(a.day_pnl)}</span> · 仓位 ${fmtPct(a.exposure)}</div>`;
    const pos = a.positions || [];
    if (!pos.length) html += `<div class="empty">无持仓</div>`;
    else {
      html += `<table><thead><tr><th>标的</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>浮盈</th></tr></thead><tbody>`;
      for (const p of pos) {
        html += `<tr><td>${p.name || p.symbol}</td><td>${p.quantity}</td><td>${p.avg_cost ?? "—"}</td>
          <td>${p.last_price ?? "—"}</td><td>${fmtMoney(p.market_value)}</td>
          <td class="${signClass(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl)}</td></tr>`;
      }
      html += `</tbody></table>`;
    }
  } else {
    html += `<div class="msg err">该账户当前不在节点最新数据中(可能已删除或节点离线)。</div>`;
  }

  const trades = allTrades.filter((t) => t.account_id === accountId);
  html += `<div class="section-title">最近成交</div>`;
  if (!trades.length) html += `<div class="empty">无成交记录</div>`;
  else {
    html += `<table><thead><tr><th>时间</th><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>净额</th><th>已实现</th></tr></thead><tbody>`;
    for (const t of trades.slice(0, 30)) {
      if (t.kind !== "trade") {
        html += `<tr><td>${(t.timestamp || "").slice(5, 16)}</td><td>${t.symbol || "—"}</td><td colspan="5">${t.kind} · ${t.reason || ""}</td></tr>`;
        continue;
      }
      html += `<tr><td>${(t.timestamp || "").slice(5, 16)}</td><td>${t.name || t.symbol}</td>
        <td class="${t.side === "BUY" ? "up" : "down"}">${t.side}</td><td>${t.quantity}</td><td>${t.price}</td>
        <td>${fmtMoney(t.net_cash)}</td><td class="${signClass(t.realized_pnl)}">${t.realized_pnl == null ? "—" : fmtMoney(t.realized_pnl)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }
  return html;
}

// ---- 表单弹窗:添加节点 / 远程开户 -------------------------------------
function showForm(title, bodyHtml) {
  $("#form-title").textContent = title;
  $("#form-body").innerHTML = bodyHtml;
  $("#form-modal").hidden = false;
}

function addNodeForm() {
  showForm("添加节点", `
    <div class="form-row"><label for="f-name">显示名</label><input id="f-name" name="name" placeholder="如 Alice…" autocomplete="off"></div>
    <div class="form-row"><label for="f-url">节点地址 base_url</label><input id="f-url" name="base_url" type="url" inputmode="url" placeholder="如 http://192.168.1.23:8000…" autocomplete="off" spellcheck="false"></div>
    <div class="form-row"><label for="f-ds">数据源 data_source(可空,自动取节点默认)</label><input id="f-ds" name="data_source" placeholder="如 tongdaxin…" autocomplete="off" spellcheck="false"></div>
    <div class="form-row"><label for="f-token">节点 token(可空)</label><input id="f-token" name="token" placeholder="如 admin-token…" autocomplete="off" spellcheck="false"></div>
    <div id="f-msg" aria-live="polite"></div>
    <div class="form-actions"><button class="btn-primary" id="f-submit" type="button">添加</button></div>`);
  $("#f-submit").onclick = async () => {
    try {
      await api("POST", "/api/admin/nodes", {
        name: $("#f-name").value.trim(), base_url: $("#f-url").value.trim(),
        data_source: $("#f-ds").value.trim() || null, token: $("#f-token").value.trim() || null,
      });
      $("#form-modal").hidden = true;
      refresh();
    } catch (e) { $("#f-msg").innerHTML = `<div class="msg err">${e.message}</div>`; }
  };
}

function openAccountForm(nodeId, name) {
  showForm(`远程开户 → ${name}`, `
    <div class="form-row"><label for="a-name">账户名</label><input id="a-name" name="account_name" placeholder="如 新同事…" autocomplete="off"></div>
    <div class="form-row"><label for="a-cash">初始资金</label><input id="a-cash" name="initial_cash" type="number" inputmode="numeric" min="0" value="10000000"></div>
    <div id="a-msg" aria-live="polite"></div>
    <div class="form-actions"><button class="btn-primary" id="a-submit" type="button">提交开户</button></div>`);
  $("#a-submit").onclick = async () => {
    try {
      const r = await api("POST", `/api/admin/nodes/${nodeId}/control`, {
        method: "POST", path: "/api/accounts",
        body: { name: $("#a-name").value.trim() || "Paper Account", initial_cash: Number($("#a-cash").value) },
      });
      $("#a-msg").innerHTML = `<div class="msg ok">开户成功:${(r.result.account || {}).id || "ok"}</div>`;
    } catch (e) { $("#a-msg").innerHTML = `<div class="msg err">${e.message}</div>`; }
  };
}

// ---- 主循环:优先 SSE 推送,断连时退回轮询 -------------------------------
function renderAll(d) {
  renderTicker(d);
  renderTotals(d.totals);
  renderWall(d.accounts || [], d.nodes || []);
  renderLeaderboard(d.leaderboard);
  renderAlerts(d.alerts);
}

// 轮询兜底
let timer = null;
async function refresh() {
  try {
    const d = await api("GET", "/api/admin/overview");
    renderAll(d);
    if (!es || es.readyState !== 1) { $("#conn").textContent = "轮询兜底"; $("#conn").className = "pill"; }
  } catch (e) {
    $("#conn").textContent = "Admin 连接失败"; $("#conn").className = "pill bad";
  }
}
function startPolling() { if (timer) clearInterval(timer); timer = setInterval(refresh, Number($("#interval").value)); }
function stopPolling() { if (timer) { clearInterval(timer); timer = null; } }

// SSE 实时推送(EventSource 自带断线重连)
let es = null;
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/api/admin/events");
  es.addEventListener("overview", (e) => {
    try { renderAll(JSON.parse(e.data)); } catch (_) {}
    $("#conn").textContent = "实时"; $("#conn").className = "pill ok";
    stopPolling();  // SSE 通了就不用轮询
  });
  es.onopen = () => stopPolling();
  es.onerror = () => {  // SSE 断 → 退回轮询;EventSource 会自动重连
    $("#conn").textContent = "重连中…"; $("#conn").className = "pill";
    startPolling();
  };
}

// 事件绑定
$("#interval").onchange = () => { if (timer) startPolling(); };  // 仅影响兜底轮询频率
$("#add-node").onclick = addNodeForm;
$("#modal-close").onclick = () => ($("#modal").hidden = true);
$("#form-close").onclick = () => ($("#form-modal").hidden = true);
$("#modal").onclick = (e) => { if (e.target.id === "modal") $("#modal").hidden = true; };
$("#form-modal").onclick = (e) => { if (e.target.id === "form-modal") $("#form-modal").hidden = true; };
document.addEventListener("keydown", (e) => {  // Esc 关闭任意打开的弹窗
  if (e.key === "Escape") { $("#modal").hidden = true; $("#form-modal").hidden = true; }
});

refresh();      // 首屏立即拉一份
connectSSE();   // 之后由 SSE 推送驱动,断连自动退回轮询
