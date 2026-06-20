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
  const color = values[values.length - 1] >= values[0] ? "var(--up)" : "var(--down)";
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline fill="none" stroke="${color}" stroke-width="1.5" points="${pts}" /></svg>`;
}

// ---- 渲染:监控墙 -------------------------------------------------------
function renderTotals(t) {
  $("#totals").innerHTML = `
    <div class="kv"><b class="${signClass(t.pnl)}">${fmtMoney(t.pnl)}</b><span>总盈亏</span></div>
    <div class="kv"><b>${fmtMoney(t.equity)}</b><span>总净值</span></div>
    <div class="kv"><b>${t.online}/${t.node_count}</b><span>在线节点</span></div>
    <div class="kv"><b>${t.position_count}</b><span>总持仓数</span></div>`;
}

function renderWall(nodes) {
  const wall = $("#wall");
  wall.innerHTML = "";
  if (!nodes.length) { wall.appendChild(el("div", "empty", "还没有节点。点右上角「+ 添加节点」,或让节点自注册。")); return; }
  for (const n of nodes) {
    const card = el("div", "card " + (n.status === "offline" ? "offline" : ""));
    const stale = n.status !== "online" && n.last_ok_at ? `<span class="stale">最后已知 ${ago(n.last_ok_at)}</span>` : `<span class="ds">${n.data_source || ""}</span>`;
    card.innerHTML = `
      <div class="head">
        <span class="dot ${n.status}"></span>
        <span class="name">${n.name}</span>
        ${stale}
      </div>
      <div class="metrics">
        <div class="metric"><span>总收益率</span><b class="${signClass(n.pnl_pct)}">${fmtPct(n.pnl_pct)}</b></div>
        <div class="metric"><span>当日盈亏</span><b class="${signClass(n.day_pnl)}">${fmtMoney(n.day_pnl)}</b></div>
        <div class="metric"><span>净值</span><b>${fmtMoney(n.equity)}</b></div>
        <div class="metric"><span>仓位</span><b>${fmtPct(n.exposure)}</b></div>
      </div>
      ${sparkline(n.spark)}
      <div class="foot">
        <span>${n.account_count ?? "—"} 账户 · ${n.position_count ?? "—"} 持仓</span>
        <span>${n.status === "online" ? (n.latency_ms ?? "—") + "ms" : (n.last_error ? "离线" : "—")}</span>
      </div>`;
    card.onclick = () => openDetail(n.id, n.name);
    wall.appendChild(card);
  }
}

function renderLeaderboard(rows) {
  const lb = $("#leaderboard");
  lb.innerHTML = "";
  if (!rows.length) { lb.appendChild(el("li", "empty", "暂无在线数据")); return; }
  for (const r of rows) {
    const li = el("li");
    li.innerHTML = `<span class="lname">${r.name}</span>
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
      <button class="ack" data-id="${a.id}">确认</button>`;
    li.querySelector(".ack").onclick = async (e) => {
      e.stopPropagation();
      await api("POST", `/api/admin/alerts/${a.id}/ack`);
      refresh();
    };
    box.appendChild(li);
  }
}

// ---- 下钻弹窗 -----------------------------------------------------------
async function openDetail(nodeId, name) {
  $("#modal-title").textContent = name;
  $("#modal-body").innerHTML = "加载中…";
  $("#modal").hidden = false;
  try {
    const [detail, trades] = await Promise.all([
      api("GET", `/api/admin/nodes/${nodeId}`),
      api("GET", `/api/admin/nodes/${nodeId}/trades`),
    ]);
    $("#modal-body").innerHTML = renderDetail(nodeId, detail, trades.trades || []);
    wireDetail(nodeId, name);
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="msg err">${e.message}</div>`;
  }
}

function renderDetail(nodeId, detail, trades) {
  const accounts = (detail.summary && detail.summary.accounts) || [];
  let html = `<div class="form-actions" style="justify-content:flex-start">
      <button class="btn-primary" id="act-open-account">远程开户</button>
      <button class="btn-danger" id="act-delete-node">删除节点</button>
    </div>`;

  for (const a of accounts) {
    html += `<div class="section-title">账户 ${a.name} · 净值 ${fmtMoney(a.equity)} · 收益 <span class="${signClass(a.pnl_pct)}">${fmtPct(a.pnl_pct)}</span></div>`;
    const pos = a.positions || [];
    if (!pos.length) { html += `<div class="empty">无持仓</div>`; continue; }
    html += `<table><thead><tr><th>标的</th><th>数量</th><th>成本</th><th>现价</th><th>市值</th><th>浮盈</th></tr></thead><tbody>`;
    for (const p of pos) {
      html += `<tr><td>${p.name || p.symbol}</td><td>${p.quantity}</td><td>${p.avg_cost ?? "—"}</td>
        <td>${p.last_price ?? "—"}</td><td>${fmtMoney(p.market_value)}</td>
        <td class="${signClass(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl)}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  html += `<div class="section-title">最近成交</div>`;
  if (!trades.length) { html += `<div class="empty">无成交记录</div>`; }
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

function wireDetail(nodeId, name) {
  $("#act-delete-node").onclick = async () => {
    if (!confirm(`确认从监控墙移除节点「${name}」?(不影响该节点交易)`)) return;
    await api("POST", `/api/admin/nodes/${nodeId}/delete`);
    $("#modal").hidden = true;
    refresh();
  };
  $("#act-open-account").onclick = () => openAccountForm(nodeId, name);
}

// ---- 表单弹窗:添加节点 / 远程开户 -------------------------------------
function showForm(title, bodyHtml) {
  $("#form-title").textContent = title;
  $("#form-body").innerHTML = bodyHtml;
  $("#form-modal").hidden = false;
}

function addNodeForm() {
  showForm("添加节点", `
    <div class="form-row"><label>显示名</label><input id="f-name" placeholder="Alice"></div>
    <div class="form-row"><label>节点地址 base_url</label><input id="f-url" placeholder="http://192.168.1.23:8000"></div>
    <div class="form-row"><label>数据源 data_source(可空,自动取节点默认)</label><input id="f-ds" placeholder="akshare"></div>
    <div class="form-row"><label>节点 token(可空)</label><input id="f-token" placeholder="admin-token"></div>
    <div id="f-msg"></div>
    <div class="form-actions"><button class="btn-primary" id="f-submit">添加</button></div>`);
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
    <div class="form-row"><label>账户名</label><input id="a-name" placeholder="新同事"></div>
    <div class="form-row"><label>初始资金</label><input id="a-cash" type="number" value="10000000"></div>
    <div id="a-msg"></div>
    <div class="form-actions"><button class="btn-primary" id="a-submit">提交开户</button></div>`);
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
  renderTotals(d.totals);
  renderWall(d.nodes);
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

refresh();      // 首屏立即拉一份
connectSSE();   // 之后由 SSE 推送驱动,断连自动退回轮询
