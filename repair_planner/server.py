"""维修规划 HTTP 服务（仅标准库）。

端点：
  GET  /api/scenarios            预置场景列表
  POST /api/session              用预置场景或自定义参数创建会话
  POST /api/session/step         执行下一步（真实运算，结果反馈规划状态）
  POST /api/session/run          连续执行到结束/受阻/不可行
  POST /api/session/reset        重置当前会话
  GET  /api/session              会话快照（真世界/信念/计划/审计/轨迹）
  GET  /                         窗口界面（静态页）
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from pathlib import Path

from .executor import Engine
from .scenario import Scenario, scenarios, TOOL_DEFAULT, FAULT_TYPES

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_LOCK = threading.Lock()
ENGINES: dict[str, Engine] = {}
_SEQ = {"id": 0}


def _new_engine(payload: dict) -> Engine:
    if "scenario" in payload and payload["scenario"]:
        scn = scenarios()[payload["scenario"]]
    else:
        tool_at = dict(TOOL_DEFAULT)
        tool_at.update(payload.get("tool_at", {}))
        # 移除工具：从 tool_at 中剔除
        for t in payload.get("remove_tools", []):
            tool_at.pop(t, None)
        scn = Scenario(
            name=payload.get("name", "自定义场景"),
            fault=payload.get("fault", "valve_worn"),
            tool_at=tool_at,
            box_taken_by_mate=bool(payload.get("box_taken_by_mate", True)),
            donor_valve_available=bool(payload.get("donor_valve_available", False)),
            gasket_available=bool(payload.get("gasket_available", True)),
            seal_intact=bool(payload.get("seal_intact", True)),
            description=payload.get("description", "界面自定义场景"),
        )
    eng = Engine(scn)
    eng.replan()
    return eng


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/scenarios":
            return self._send(200, {
                "scenarios": [
                    {"key": k, "name": v.name, "fault": v.fault,
                     "box_taken_by_mate": v.box_taken_by_mate,
                     "donor_valve_available": v.donor_valve_available,
                     "gasket_available": v.gasket_available,
                     "seal_intact": v.seal_intact,
                     "tool_at": v.tool_at,
                     "description": v.description}
                    for k, v in scenarios().items()
                ],
                "fault_types": list(FAULT_TYPES),
                "tools": list(TOOL_DEFAULT.keys()),
                "places": ["office", "corridor", "pump_room", "storage", "workshop"],
            })
        if path == "/api/session":
            with _LOCK:
                eng = ENGINES.get("current")
            if eng is None:
                return self._send(404, {"error": "无活动会话"})
            return self._send(200, eng.snapshot())
        if path in ("/", "/index.html"):
            page = (WEB_DIR / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/app.js":
            js = (WEB_DIR / "app.js").read_bytes()
            return self._send(200, js, "application/javascript; charset=utf-8")
        if path == "/styles.css":
            css = (WEB_DIR / "styles.css").read_bytes()
            return self._send(200, css, "text/css; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, {"error": "非法 JSON"})

        with _LOCK:
            if path == "/api/session":
                _SEQ["id"] += 1
                try:
                    eng = _new_engine(payload)
                except KeyError as e:
                    return self._send(400, {"error": f"未知场景/参数: {e}"})
                ENGINES["current"] = eng
                return self._send(200, eng.snapshot())

            eng = ENGINES.get("current")
            if eng is None:
                return self._send(404, {"error": "无活动会话，请先创建"})

            if path == "/api/session/reset":
                scn = eng.scn
                eng = Engine(scn)
                eng.replan()
                ENGINES["current"] = eng
                return self._send(200, eng.snapshot())

            if path == "/api/session/step":
                if eng.finished or eng.failed:
                    return self._send(200, eng.snapshot())
                eng.step()
                return self._send(200, eng.snapshot())

            if path == "/api/session/run":
                limit = int(payload.get("max_steps", 40))
                eng.run_all(max_steps=limit)
                return self._send(200, eng.snapshot())

        self._send(404, {"error": "not found"})


def serve(host: str = "127.0.0.1", port: int = 8765):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"维修规划窗口服务已启动: http://{host}:{port}/")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
