# 节点端「小改」接入说明

落在现有 app(`goal-here-is-the-thing-app`)。改动极小、**默认关闭**——不设环境变量时,节点行为与改之前完全一致。

> 监控墙 MVP **不需要这个 patch**:只要节点以 `HOST=0.0.0.0` 启动、Admin 能连到,就能被监控。
> 这个 patch 解决两件锦上添花的事:**自注册**(免去在 Admin 手填每台 IP)和**写鉴权**(反向控制开户时校验 token)。

## 步骤

### 1. 放文件
把本目录的 `admin_link.py` 复制到节点仓库的 `backend/admin_link.py`。

### 2. 改 `backend/server.py` —— 共 3 处小改

**(a) 顶部 import:**
```python
from backend import admin_link
```

**(b) `do_POST` 入口加一行鉴权**(在 `def do_POST(self)` 第一句):
```python
    def do_POST(self) -> None:
        if not admin_link.check_admin_token(self):
            return            # 设了 NODE_TOKEN 且 token 不符 → 已返回 401
        parsed = urlparse(self.path)
        ...
```
> 同理可加到 `do_DELETE`/`do_PUT`(如有)。GET 只读不需要鉴权,监控才能裸拉。

**(c) `run()` 里启动自注册**(在 `server.serve_forever()` 之前):
```python
    admin_link.self_register()
    server.serve_forever()
```

### 3. 启动节点(局域网 + 自注册 + 鉴权)
```bash
HOST=0.0.0.0 \
PORT=8000 \
LAN_IP=192.168.1.23 \
NODE_ID=alice-mbp \
NODE_NAME=Alice \
NODE_TOKEN=alice-secret-xyz \
ADMIN_URL=http://192.168.1.10:8800 \
python3 -m backend.server
```
- `HOST=0.0.0.0`:让局域网可达(现有 server.py 已支持该 env)。
- `NODE_TOKEN`:设了才开启写鉴权;Admin 反向控制时会带回这个 token。
- `ADMIN_URL`:设了才自注册;Admin 侧若开了 `ADMIN_TOKEN`,这里还需 `ADMIN_TOKEN=<共享密钥>`。

### 4. (可选, Phase 3b) SSE 事件流 —— 真·实时

让成交"秒级"上墙,而不是等下一轮轮询(2–5s)。两处小改:

**(a) `do_GET` 里加一条路由**(放在其它 GET 路由之间即可):
```python
            if path == "/api/events/stream":
                admin_link.stream_sse(self)
                return
```

**(b) 在事件落库处广播一个信号**。最完整的位置是审计事件入口 `backend/audit_store.py`
的 `record_event(...)`,在它 `return event_id` 之前加:
```python
        from backend import admin_link
        admin_link.publish_event(event.event_type, symbol=getattr(event, "symbol", None))
```
> `publish_event` 内部已 try/except 全吞 —— **绝不会**因为发信号而影响交易/落账。
> 不想动 `audit_store` 的话,退而求其次:在 `server.py` 的 `do_POST` 成功返回前调
> `admin_link.publish_event("post")`,能覆盖经 HTTP 下单/开户的变更(但覆盖不到策略/调度线程里产生的成交)。

接入后:Admin 会自动为该节点起一条 SSE 消费线程,收到信号立即重拉。
**没接 SSE 的节点不受影响** —— Admin 探测到没有 `/api/events/stream` 就退回周期轮询。

## 行为对照

| 场景 | 不设任何 env | 设了对应 env |
|---|---|---|
| 监听地址 | `127.0.0.1`(仅本机) | `0.0.0.0`(局域网) |
| 写鉴权 | 无(任何人可 POST) | 需 `X-Admin-Token == NODE_TOKEN` |
| 自注册 | 不注册 | 启动后台线程注册到 Admin |

全部向后兼容:这是"演进而不打破"——不配置就退化成原行为。
