#!/usr/bin/env bash
# 窗口启动步骤：在项目根目录执行本脚本，随后用浏览器访问提示地址。
# 终端辅助验证请改用：python3 repair_cli.py list|plan|run
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8765}"
export REPAIR_PORT="$PORT"
echo "[1/2] 检查 Python 运行环境..."
python3 -c "import http.server, json, heapq, dataclasses"
echo "[2/2] 启动维修规划窗口服务（端口 $PORT）..."
exec python3 run_window.py
