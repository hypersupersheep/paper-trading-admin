# Phase 0 — 契约 (Contracts) 与数据模型

本文件是 Admin 与节点之间的**唯一权威契约**。任何一侧改动接口,先改这里。

目录
- [1. 节点侧契约 (Node API,Admin 只消费)](#1-节点侧契约)
- [2. Admin 自身数据模型 (SQLite)](#2-admin-数据模型)
- [3. Admin API (前端/节点消费)](#3-admin-api)
- [4. 节点自注册协议](#4-节点自注册协议)
- [5. 告警模型](#5-告警模型)
- [6. 兼容性约定](#6-兼容性约定)

---

## 1. 节点侧契约

节点 = 现有 `goal-here-is-the-thing-app`。`API_VERSION = 1`,`ThreadingHTTPServer`,默认 `:8000`。
**监控 MVP 不要求节点改任何 API**;只需节点以 `HOST=0.0.0.0` 启动,让 Admin 在局域网可达。

Admin 在每个轮询周期对每个节点拉以下端点(全部 GET,只读):

### 1.1 `GET /api/health`
```json
{ "status": "ok", "database": "/path/to/app.db" }
```
判活基线。HTTP 200 且 `status=="ok"` 视为在线。

### 1.2 `GET /api/meta`
能力发现。Admin 据 `api_version` 做兼容判断,缓存 `name/version/data_sources/default_data_source`。
```json
{
  "name": "量化模拟盘 Paper Trading",
  "version": "1.7.0",
  "api_version": 1,
  "data_home": "...",
  "data_sources": ["akshare", "..."],
  "default_data_source": "akshare",
  "capabilities": { "accounts": true, "paper_broker": true, "...": true },
  "endpoints": { "...": "..." }
}
```

### 1.3 `GET /api/portfolio/summary?data_source=<ds>&frequency=5m`
监控墙主数据源。**必须带 `data_source`**(取节点 `meta.default_data_source`),否则不做实时盯市、且不返回 `day_pnl`。
不传 `account_id` = 返回该节点全部账户聚合。

```json
{
  "accounts": [
    {
      "id": "acct_xxx", "name": "Paper Account",
      "currency": "CNY", "market": "CN_A",
      "initial_cash": 10000000.0,
      "total_cash": 9000000.0, "market_value": 1100000.0,
      "unrealized_pnl": 50000.0, "holdings_day_pnl": 12000.0,
      "equity": 10100000.0, "pnl": 100000.0, "pnl_pct": 0.01,
      "exposure": 0.108,
      "day_pnl": 15000.0, "day_realized_pnl": 3000.0,   // 仅带 data_source 时
      "sleeves": [ ... ], "positions": [ ... ]
    }
  ],
  "totals": {
    "initial_cash": ..., "equity": ..., "pnl": ..., "pnl_pct": ...,
    "market_value": ..., "exposure": ...,
    "account_count": 1, "position_count": 7
  },
  "mark": { "mode": "connector_close", "data_source": "akshare", "marked_symbols": [...] }
}
```
> Admin 节点级指标取 `totals`(单一真相):`equity / pnl / pnl_pct / exposure / account_count / position_count`。
> `day_pnl` 在 `totals` 里没有 → Admin 对 `accounts[].day_pnl` 求和得节点级当日盈亏。

`positions[]` 单条(下钻用):
```json
{ "symbol": "600519.SH", "name": "贵州茅台", "quantity": 100,
  "avg_cost": 1600.0, "last_price": 1700.0, "market_value": 170000.0,
  "cost_basis": 160000.0, "unrealized_pnl": 10000.0, "day_pnl": 800.0 }
```

### 1.4 `GET /api/audit/trades?limit=50`
最近成交流(下钻 + "新成交/大额成交"告警)。每行已折叠成一笔。
```json
{ "trades": [
  { "kind": "trade", "id": "evt_...", "timestamp": "2026-06-17T01:30:00+08:00",
    "account_id": "acct_xxx", "symbol": "600519.SH", "name": "贵州茅台",
    "side": "BUY", "quantity": 100, "price": 1700.0,
    "gross_amount": 170000.0, "fees": 13.6, "net_cash": -170013.6,
    "position_after": 100, "realized_pnl": null, "voided": false, "reason": "..." },
  { "kind": "order_rejected", "id": "...", "...": "..." }   // 未成交链保留头条
] }
```
Admin 以 `id` 去重判定"新成交"(`kind=="trade" && !voided`)。

### 1.5 (可选, Phase 3b) `GET /api/events/stream` — SSE 变更信号
节点装了 [`node_patch`](node_patch/PATCH.md) 后暴露。`text/event-stream`,成交/审计事件发生时推:
```
event: change
data: {"type": "trade_filled", "ts": 1750.., "symbol": "600519.SH"}
```
**信号只表示"该节点有变更",不搬运业务数据** —— Admin 收到即对该节点立即重拉 §1.3/§1.4。
节点没有此端点(404)时,Admin 退回周期轮询,功能不降级、只是延迟到下一轮。

### 1.6 反向控制目标:`POST /api/accounts`
开户。请求体(全部可选,有缺省):
```json
{ "name": "Bob", "initial_cash": 10000000, "currency": "CNY", "market": "CN_A" }
```
返回 `201 { "account": { "id": "...", ... } }`。
> 节点当前**无鉴权**。生产中应配合 [`node_patch`](node_patch/PATCH.md) 加 `admin-token`,Admin 代理时带 `X-Admin-Token`。

---

## 2. Admin 数据模型

Admin 自己的 SQLite(`data/admin.db`),与节点 DB 完全隔离。建表见 `admin/db.py`。

### `nodes` — 节点注册表
| 列 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 稳定节点标识(hostname 或自定义,如 `alice`) |
| `name` | TEXT | 显示名 |
| `base_url` | TEXT | `http://192.168.x.x:8000`(无尾斜杠) |
| `token` | TEXT | 调用该节点写接口用的 admin-token(自注册时上报) |
| `data_source` | TEXT | 盯市数据源;空则轮询时用节点 `meta.default_data_source` |
| `api_version` | INT | 最近一次 meta 的 api_version |
| `enabled` | INT | 1=纳入轮询 |
| `created_at` / `updated_at` | TEXT | ISO8601 |

### `node_state` — 每节点最新快照(覆盖写;离线兜底 + 首屏)
| 列 | 说明 |
|---|---|
| `node_id` PK | |
| `status` | `online` / `offline` / `degraded` |
| `last_ok_at` | 最后一次成功拉取(ISO) |
| `last_error` | 最近错误文本 |
| `latency_ms` | 最近一次往返耗时 |
| `consecutive_fail` | 连续失败次数(离线判定/告警去抖) |
| `equity`/`pnl`/`pnl_pct`/`day_pnl`/`exposure`/`position_count`/`account_count` | 从 totals 提取的扁平指标(排行榜直接读) |
| `summary_json` / `trades_json` / `meta_json` | 最近原文(下钻 + 离线展示) |
| `updated_at` | |

### `equity_samples` — 净值时序(sparkline / 走势 / 回看)
`(id PK, node_id, ts, equity, pnl, pnl_pct, day_pnl, exposure, position_count)`,索引 `(node_id, ts)`。
每 `SAMPLE_EVERY` 秒落一点(非每轮),避免膨胀。

### `alerts` — 告警日志
`(id PK, node_id, ts, severity, rule, message, context_json, acknowledged)`,索引 `(ts)`。

---

## 3. Admin API

前缀 `/api/admin`。读端点开放;写端点(注册/控制)在设了 `ADMIN_TOKEN` 时需请求头 `X-Admin-Token`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/overview` | 监控墙首屏:`{nodes:[扁平state], totals, leaderboard, alerts(未确认)}` |
| GET | `/api/admin/nodes` | 注册表 |
| POST | `/api/admin/nodes` | 手动加节点(body 见 §2 nodes) |
| POST | `/api/admin/nodes/{id}/delete` | 删节点(连带 state/samples 不删历史 alerts) |
| GET | `/api/admin/nodes/{id}` | 下钻:state + 最近 summary(账户/持仓)+ meta |
| GET | `/api/admin/nodes/{id}/trades` | 最近成交(取缓存 trades_json) |
| GET | `/api/admin/nodes/{id}/history?limit=` | equity_samples 序列 |
| POST | `/api/admin/nodes/{id}/control` | **反向控制代理**:`{method,path,body}` → 转发到节点(带其 token) |
| POST | `/api/admin/register` | 节点自注册(见 §4) |
| GET | `/api/admin/alerts?limit=&unack=1` | 告警列表 |
| POST | `/api/admin/alerts/{id}/ack` | 确认告警 |
| GET | `/api/admin/events` | **SSE**:每轮/每次重拉后推一帧 `event: overview`(数据同 overview);心跳 `: keepalive`。前端 EventSource 消费,断连退回轮询。 |

`/control` 是唯一对节点的写通道,显式、用户触发。例:开户
```json
POST /api/admin/nodes/alice/control
{ "method": "POST", "path": "/api/accounts", "body": { "name": "新同事", "initial_cash": 10000000 } }
```

---

## 4. 节点自注册协议

节点小改后,启动时 `POST {ADMIN_URL}/api/admin/register`:
```json
{ "id": "alice-mbp", "name": "Alice", "base_url": "http://192.168.1.23:8000",
  "token": "<该节点的 admin-token>", "api_version": 1 }
```
- Admin 若设了 `ADMIN_TOKEN`,节点须带 `X-Admin-Token`(共享密钥)。
- 幂等:同 `id` 重复注册 = 更新 `base_url/token/api_version` + 重新 `enabled`。
- 注册即纳入下一轮轮询。节点无需此步也能被监控(手动加即可),自注册只是免去手填 IP。

---

## 5. 告警模型

**模拟盘只做连通性告警,不做任何交易阈值告警**(回撤/当日亏/仓位/大额成交都不触发)——
因为这是 paper trading 监控,盈亏本身不是风险事件;真正要让老板知道的是"某节点掉线了/又回来了"。

每轮轮询后对每个节点求值。**边沿触发**:仅状态翻转时落一条 `alert`,不每轮刷屏(`admin/alerts.py`)。

| rule | severity | 触发(进入) | 恢复 |
|---|---|---|---|
| `node_offline` | critical | `consecutive_fail >= OFFLINE_AFTER_FAILS`(默认 2) | 重新在线 → `node_recovered`(info) |

> 盈亏/仓位/成交等数据仍在监控墙与下钻里**展示**,只是不产生告警。
> 若将来要加阈值告警,在 `admin/alerts.py` 扩 `AlertEngine.evaluate` 即可。

---

## 6. 兼容性约定

- Admin 读节点 `meta.api_version`;`> 已知大版本` 时该节点卡片标"协议过新,部分功能不可用",但仍尽量展示 health/totals。
- 节点字段缺失一律 best-effort:取不到 `day_pnl` 显示 `—`,不报错、不影响其它节点。
- 一个节点慢/挂**绝不阻塞**其它节点:轮询并发 + 单节点超时隔离。
