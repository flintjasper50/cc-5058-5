#!/usr/bin/env python3
"""窗口启动入口：启动 HTTP 服务并尝试打开浏览器窗口。

终端辅助任务请用 repair_cli.py，本文件只负责窗口。
"""
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repair_planner.server import serve

PORT = int(os.environ.get("REPAIR_PORT", "8765"))


def _open_browser():
    url = f"http://127.0.0.1:{PORT}/"
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    print("=" * 64)
    print("维修角色目标规划器 - 窗口界面")
    print(f"浏览器打开: http://127.0.0.1:{PORT}/")
    print("若浏览器未自动弹出，请手动复制上面的地址。")
    print("=" * 64)
    threading.Timer(0.8, _open_browser).start()
    serve(port=PORT)
