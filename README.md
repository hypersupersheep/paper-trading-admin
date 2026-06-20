# Paper Trading Admin

模拟盘集中监控端。运行在局域网内一台常开机器上,主动连接各节点(同事正在运行的 paper trading app),聚合净值、盈亏、持仓与成交,提供实时监控墙、排行榜、明细下钻、掉线告警与远程开户。Admin 自身不参与交易。

## 功能

- **监控墙** —— 每个节点的净值走势、总收益率、当日盈亏、仓位
- **排行榜** —— 按收益率排序
- **下钻** —— 单节点的账户、持仓与最近成交
- **告警** —— 节点离线 / 恢复
- **远程开户** —— 向指定节点创建账户
- **实时** —— 成交事件秒级反映(SSE);无事件时回落到 2–5s 轮询

## 运行

### 发布版

在 [Releases](../../releases) 下载对应平台压缩包,解压后运行,启动后访问 `http://localhost:8800`:

- macOS:`ptadmin-macos-arm64.zip`
- Windows:`ptadmin-windows-x64.zip`

### 源码

Python 3.10+,无第三方依赖:

```bash
python3 -m admin
```

## 接入节点

节点以局域网可达方式启动:

```bash
HOST=0.0.0.0 python3 -m backend.server
```

在监控墙「添加节点」中填入名称与地址(如 `http://192.168.1.23:8000`)即可纳入监控,**节点无需改动 API**。

自注册、SSE 实时推送、写操作鉴权属可选增强,需对节点打补丁(默认关闭、向后兼容),见 [`node_patch/PATCH.md`](node_patch/PATCH.md)。

## 隔离

- Admin 仅读取节点数据;唯一的写操作是显式触发的远程开户。
- Admin 宕机不影响节点交易。
- 节点离线时标记状态,并保留其最后一次已知数据。

## 文档与测试

- [`CONTRACTS.md`](CONTRACTS.md) —— 接口契约与数据模型
- [`node_patch/PATCH.md`](node_patch/PATCH.md) —— 节点侧可选补丁
- `python3 -m unittest tests.test_admin`

## 配置

环境变量,均有默认值:

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PORT` | `8800` | 监听端口 |
| `ADMIN_TOKEN` | 空 | 设置后,写操作需校验该令牌 |
| `POLL_INTERVAL` | `3.0` | 轮询周期(秒) |
| `ADMIN_DB` | `data/admin.db` | Admin 数据库路径 |

完整项见 [`admin/config.py`](admin/config.py)。
