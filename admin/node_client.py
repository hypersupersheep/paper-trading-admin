"""对单个节点的 HTTP 客户端。纯 urllib,带超时与 token。

隔离要点:所有调用都有超时;任何异常都被收敛成结构化结果,绝不向上抛到轮询循环 ——
一个节点慢/挂不能拖垮整轮。读路径只发 GET;写路径(control 代理)单独走 request()。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


class NodeError(Exception):
    """节点请求失败(超时/连接拒绝/非 2xx/坏 JSON)。"""


def _normalize_base(base_url: str) -> str:
    return base_url.rstrip("/")


class NodeClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 2.5) -> None:
        self.base_url = _normalize_base(base_url)
        self.token = token or None
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[Any, int, float]:
        """发一个请求,返回 (解析后的 JSON, http_status, latency_ms)。失败抛 NodeError。"""
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            # 与 node_patch 约定的鉴权头;节点没加 token 时无害(被忽略)。
            headers["X-Admin-Token"] = self.token

        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                status = resp.status
                latency = (time.perf_counter() - started) * 1000.0
        except urllib.error.HTTPError as exc:  # 4xx/5xx:节点回了响应但非成功
            raw = exc.read()
            latency = (time.perf_counter() - started) * 1000.0
            payload = _safe_json(raw)
            raise NodeError(
                f"HTTP {exc.code} {method} {path}: "
                f"{(payload or {}).get('error') if isinstance(payload, dict) else exc.reason}"
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NodeError(f"{method} {path} 连接失败: {getattr(exc, 'reason', exc)}")

        payload = _safe_json(raw)
        if payload is None:
            raise NodeError(f"{method} {path}: 响应非 JSON")
        return payload, status, round(latency, 1)

    def get(self, path: str, timeout: float | None = None) -> tuple[Any, float]:
        payload, _status, latency = self.request("GET", path, timeout=timeout)
        return payload, latency

    def get_raw(self, path: str, timeout: float | None = None) -> tuple[int, bytes, dict[str, str]]:
        """原始字节 GET(文件透传用):返回 (status, body_bytes, headers)。带 token。"""
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        headers = {}
        if self.token:
            headers["X-Admin-Token"] = self.token
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, resp.read(), {k: v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:  # 节点回了非 2xx,把状态/体如实透传
            return exc.code, exc.read(), {k: v for k, v in exc.headers.items()}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NodeError(f"GET {path} 连接失败: {getattr(exc, 'reason', exc)}")


def _safe_json(raw: bytes) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
