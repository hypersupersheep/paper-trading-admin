"""集中读环境变量。所有配置只在这里落地一次,别处 import Config。

设计取舍:用 dataclass + from_env() 而非散落的 os.environ.get,
这样测试能直接构造 Config(...) 注入,不依赖进程环境。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓库根目录(开发态)


def _frozen() -> bool:
    """是否运行在 PyInstaller 打包出的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def _bundle_dir() -> Path:
    """打包资源(public/)所在目录:冻结时是解包临时目录,否则就是仓库根。"""
    return Path(getattr(sys, "_MEIPASS", ROOT))


def _data_root() -> Path:
    """持久数据(admin.db)落地目录:冻结时放在可执行文件旁边,保证重启不丢。"""
    return Path(sys.executable).resolve().parent if _frozen() else ROOT


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 8800
    db_path: Path = ROOT / "data" / "admin.db"
    public_dir: Path = ROOT / "public"
    admin_token: str = ""          # 空 = 写操作不鉴权(仅适合可信局域网)

    poll_interval: float = 3.0     # 轮询周期(秒);文档建议 2–5
    poll_timeout: float = 2.5      # 单节点请求超时
    poll_workers: int = 16         # 并发拉取线程数
    sample_every: float = 30.0     # 每隔多少秒落一个净值时序采样点
    offline_after_fails: int = 2   # 连续失败几次判离线

    # 模拟盘:不设交易阈值告警。仅保留连通性告警(离线/恢复)。

    @classmethod
    def from_env(cls) -> "Config":
        def _f(key: str, default: float) -> float:
            try:
                return float(os.environ[key])
            except (KeyError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            try:
                return int(os.environ[key])
            except (KeyError, ValueError):
                return default

        db = os.environ.get("ADMIN_DB")
        # 冻结(打包)运行时:DB 放可执行文件旁,public/ 从解包目录取
        default_db = (_data_root() / "data" / "admin.db") if _frozen() else cls.db_path
        default_public = (_bundle_dir() / "public") if _frozen() else cls.public_dir
        return cls(
            host=os.environ.get("ADMIN_HOST", "0.0.0.0"),
            port=_i("ADMIN_PORT", 8800),
            db_path=Path(db).expanduser().resolve() if db else default_db,
            public_dir=default_public,
            admin_token=os.environ.get("ADMIN_TOKEN", ""),
            poll_interval=_f("POLL_INTERVAL", 3.0),
            poll_timeout=_f("POLL_TIMEOUT", 2.5),
            poll_workers=_i("POLL_WORKERS", 16),
            sample_every=_f("SAMPLE_EVERY", 30.0),
            offline_after_fails=_i("OFFLINE_AFTER_FAILS", 2),
        )
