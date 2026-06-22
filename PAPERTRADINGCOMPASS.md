# Paper Trading Compass · 运行手册

> 一份「怎么把这套系统跑起来、怎么用」的操作手册。
> **维护约定**:每次大改 / 更新后同步刷新本文件。
> 当前对应版本:**Admin(账户级 + 鉴权 + SSE 事件驱动)**、**节点 app v1.12.0**。

## 目录

- [0. 这套系统是什么](#0-这套系统是什么)
- [1. 角色与端口](#1-角色与端口)
- [2. 起 Admin(监控端)](#2-起-admin监控端)
- [3. 起节点并接入 Admin(每位同事)](#3-起节点并接入-admin每位同事)
- [4. 账户怎么用](#4-账户怎么用)
- [5. 监控墙怎么看](#5-监控墙怎么看)
- [6. 两个 token 别混(安全)](#6-两个-token-别混安全)
- [7. 常见问题](#7-常见问题)
- [8. 现状:已实现 / 待做](#8-现状已实现--待做)
- [附. 命令与接口速查](#附-命令与接口速查)

---

## 0. 这套系统是什么

两个**相互独立**的程序:

- **节点(node)** = 同事在用的 paper trading app。**真正在模拟交易**,有账户、持仓、成交。
- **Admin** = 监控 / 指挥端,跑在一台常开机器上。**自己不交易**,只把各节点的账户汇总成监控墙,并能远程开户。

关系:**账户是监控单元**。节点上开一个账户 → 登记到 Admin → 出现在监控墙。Admin 挂了不影响任何人交易;节点离线则在墙上显示最后已知。

---

## 1. 角色与端口

| 程序 | 跑在哪 | 默认地址 | 角色 |
|---|---|---|---|
| Admin | 老板 / 常开机器 | `0.0.0.0:8800` | 监控墙、排行、远程开户 |
| 节点 app | 每位同事机器 | `0.0.0.0:8000` | 交易、账户、成交 |

前提:都在**同一局域网**。Admin 要能访问每个节点的 `IP:8000`;每个节点要能访问 Admin 的 `IP:8800`。

---

## 2. 起 Admin(监控端)

**源码**(需 Python 3.10+,无第三方依赖):
```bash
cd paper-trading-admin
ADMIN_TOKEN=改成你的口令 python3 -m admin
```
**或发布版**:解压 Releases 里的 `ptadmin-*.zip`,双击 `ptadmin`(macOS)/ `ptadmin.exe`(Windows)。

打开监控墙:`http://localhost:8800`(局域网内别人用 `http://<Admin机器IP>:8800`)。

常用环境变量:

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PORT` | `8800` | 端口 |
| `ADMIN_TOKEN` | 空 | **共享口令**:设了之后,登记 / 开户 / 注销等写操作要带它。生产建议设。 |
| `POLL_INTERVAL` | `15.0` | 轮询兜底周期(秒);实时走 SSE,轮询只补漏。无 SSE 的节点可调小 |
| `ADMIN_DB` | `data/admin.db` | Admin 自己的库 |

---

## 3. 起节点并接入 Admin(每位同事)

**第 1 步:让节点局域网可达**
```bash
cd <paper-trading-app>
HOST=0.0.0.0 PORT=8000 python3 -m backend.server
```
(发布版同理,确保监听 `0.0.0.0`。)

**第 2 步:把节点指向 Admin**(任选其一)

- 在节点 app 自己的页面「Admin 对接」卡里填:Admin 地址 + 共享口令 + 本机显示名。
- 或命令行:
```bash
curl -X POST http://localhost:8000/api/admin-link \
  -H 'Content-Type: application/json' \
  -d '{"admin_url":"http://<Admin机器IP>:8800","admin_token":"<和 ADMIN_TOKEN 一致>","node_name":"Alice 的机器"}'
```
- **opt-in**:不配 `admin_url` = 纯本地模式,完全不上报,行为和单机一样。

接好后:开户会自动登记,启动会自动把已有账户补登一遍。

> **数据源**:监控墙要显示实时盈亏,节点得有能用的行情数据源(节点默认 `tongdaxin`,需行情网络)。没有行情网络时可在节点用离线的 `fixture` 验证流程。

---

## 4. 账户怎么用

| 操作 | 怎么做 | 结果 |
|---|---|---|
| **开户** | 节点页面开户,或 `POST http://<节点>:8000/api/accounts {"name","owner","initial_cash"}` | 自动登记到 Admin,几秒内上墙 |
| **远程开户**(从 Admin) | 监控墙点开任一账户卡 → 「在该节点远程开户」 | Admin 代理到该节点开户 |
| **删账户** | 节点 `POST /api/accounts/{id}/delete {"force":true}` | 自动从 Admin 注销,墙上移除 |
| **重启节点** | 重启后自动 register-all | 现有账户全部补登上墙 |

**owner**:账户的交易员标识,监控墙按它分组 / 排名。开户时填;没填则回退账户名。一个 owner 可有多个账户(多策略分仓)。

---

## 5. 监控墙怎么看

- **账户卡**:每个已登记账户一张 —— owner、账户名、所属节点、总收益率、当日盈亏、净值、仓位、净值 sparkline、状态灯(绿在线 / 红离线 / 黄异常)。按 owner 排序。
- **排行榜**:右侧,按账户总收益率排名。
- **下钻**:点卡片 → 该账户的持仓明细 + 最近成交。
- **告警**:节点离线 / 恢复(模拟盘只做连通性告警,不做盈亏阈值告警)。
- **实时(事件驱动)**:节点 v1.12.0 起推 `/api/stream` SSE,有成交(`trade_filled`)/ 开户 / 删户即推;Admin 收到就**立即重拉该节点**(秒级),浏览器再经 Admin 自己的 SSE 实时刷新,顶栏显示「实时 / 重连中 / 轮询兜底」。轮询转为**兜底**(SSE 断 / 节点重启时补齐)。
  - 默认 `POLL_INTERVAL=15s`(全队已 SSE,轮询只兜底);想更省可调到 30s,某节点不支持 SSE 则对它调小。
  - SSE 事件类型(v1.12.1):`trade_filled` / `account_created` / `account_deleted` / `order_rejected`(带 reason)/ `reverse_repo`(带金额利息)。Admin 收到任意一类即重拉该节点,无需逐类处理。

---

## 6. 两个 token 别混(安全)

| token | 是什么 | 谁校验 | 现状 |
|---|---|---|---|
| **ADMIN_TOKEN** | Admin 的共享口令 | Admin 校验来访的写请求(登记 / 开户 / 注销),节点 admin-link 的 `admin_token` 要与它一致 | 设不设由你;生产建议设 |
| **node.token** | 节点自己的口令,Admin 拉**任何**接口 / 反控都要带 | 节点校验 | **v1.11.0 已启用**,节点自动生成并随登记报文给 Admin |

**v1.11.0 起节点对所有远程端点强制鉴权**:Admin 轮询(`summary`/`trades`/`meta`)和反控都带 `X-Admin-Token = 该节点 node.token`(Admin 从登记报文自动获取,无需手配)。节点本机(loopback)请求免 token。所以同网段不再裸奔。

---

## 7. 常见问题

- **账户不上墙?** ① 节点是否配了 `admin_url`(`GET http://<节点>:8000/api/admin-link` 看 `enabled`);② Admin 是否能访问节点 IP;③ `ADMIN_TOKEN` 与节点 `admin_token` 是否一致。
- **卡片显示离线 / 指标为空?** 多半是节点**数据源不通**(默认 `tongdaxin` 要行情网络)。验证流程可让节点用 `fixture`。
- **收益率全是 0?** 新账户没持仓时正常;有成交后即变化。
- **Admin 重启后空了?** 让节点重启一次(触发 register-all),或在节点调 `POST /api/admin-link/register-all` 补登。
- **节点开了鉴权(v1.11.0)后全变离线?** Admin 需要该节点的 `node.token`;让节点重启一次(register-all 会带上最新 token),Admin 拿到后轮询即恢复。

---

## 8. 现状:已实现 / 待做

**已实现并联调通过**
- 账户级登记(开户→登记、删户→注销、重启→批量补登),幂等。
- 账户级监控墙(卡片 / 排行 / 下钻 / 连通性告警 / sparkline)。
- 反向控制:从 Admin 远程开户。
- 实时:Admin → 浏览器 SSE 推送。
- **节点 admin-token 鉴权(v1.11.0)**:Admin 轮询 / 反控自动带 `node.token`,已用强制校验的 mock 节点实测(带对的→在线,带错的→离线)。
- **节点 `/api/stream` SSE 事件驱动(v1.12.0)**:Admin 为每节点起 SSE 消费线程(带 `node.token`),收到 `trade_filled`/开户/删户即重拉该节点;心跳/注释行忽略。已对真实节点核实流格式,并以强制 token 的 mock 测试连接与触发。

**待做(可选,非阻塞)**
- 节点启动自注册(低,可省 —— 登记报文已带 base_url)。
- 按需新增事件类型(风控拦截 / 逆回购等),由节点侧加、Admin 收到即重拉。

---

## 附. 命令与接口速查

**Admin**
- 监控墙:`http://<Admin>:8800`
- `GET /api/admin/overview` —— 监控墙数据(账户卡 + 排行 + 告警)
- `GET /api/admin/accounts` —— 已登记账户
- `POST /api/admin/accounts/register` —— 账户登记(节点自动调,单条或 `accounts:[]` 批量)
- `POST /api/admin/accounts/{node_id}/{account_id}/delete` —— 注销
- `POST /api/admin/nodes/{id}/control` —— 反向控制代理(远程开户走这)

**节点 app**
- `GET /api/meta` —— 握手(版本 / 能力),`api_version=1`
- `POST /api/admin-link` —— 配置对接 Admin(`admin_url` / `admin_token` / `node_name`)
- `POST /api/accounts` —— 开户(`name` / `owner` / `initial_cash`)
- `POST /api/accounts/{id}/delete` —— 删户
- `POST /api/admin-link/register-all` —— 手动批量补登
