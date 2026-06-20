#!/usr/bin/env bash
# 启动 Paper Trading Admin。环境变量见 README / admin/config.py。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m admin
