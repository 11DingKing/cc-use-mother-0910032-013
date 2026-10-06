"""JSON HTTP API（仅依赖标准库）。

路由::

    POST   /api/spaces                创建空间节点
    GET    /api/spaces                空间列表
    GET    /api/spaces/{id}           空间详情（布局/关闭）
    GET    /api/space-tree            空间层级树
    POST   /api/layouts               新增布局版本（含区域、容量、设备）
    POST   /api/layouts/activate      布局切换（转换窗口原子检测）
    POST   /api/closures              部分/整体关闭
    POST   /api/events                创建活动（筹备）
    GET    /api/events                活动列表
    GET    /api/events/{id}           活动详情（含占用快照）
    PATCH  /api/events/{id}           修改筹备中活动
    POST   /api/events/{id}/submit    提交确认（→待确认）
    POST   /api/events/{id}/confirm   并发确认（→已排定，支持版本 CAS）
    POST   /api/events/{id}/start     开始执行
    POST   /api/events/{id}/settle    完成结算（保留原布局）
    POST   /api/events/{id}/cancel    取消
    POST   /api/availability          试排期：展开占用、冲突路径、替代空间

冲突响应（409）示例载荷::

    {
      "error": "conflict",
      "message": "...",
      "conflicts": [
        {
          "type": "event",
          "relationship": "祖先空间已被占用",
          "request_path": [{"space_id": "...", "name": "..."}],
          "occupied_path": [...],
          "window": {"start": "...", "end": "..."},
          "event_id": "E0001", "zone_name": "...", "layout_name": "..."
        }
      ],
      "alternatives": [{"space_id": ..., "zone_id": ..., "capacity": ...}]
    }
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .errors import VenueError
from .service import VenueService


class ApiState:
    def __init__(self, service: VenueService | None = None) -> None:
        self.service = service or VenueService()


def _make_handler(state: ApiState) -> type[BaseHTTPRequestHandler]:
    class VenueHandler(BaseHTTPRequestHandler):
        server_version = "VenueScheduling/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            if getattr(self.server, "quiet", False):
                return
            super().log_message(fmt, *args)

        # ---- 基础收发 ---------------------------------------------------
        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise VenueError("请求体不是合法 JSON") from exc
            if not isinstance(data, dict):
                raise VenueError("请求体必须是 JSON 对象")
            return data

        def _send(self, status: int, body: Any) -> None:
            payload = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # ---- 路由 -------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            self._route("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._route("POST")

        def do_PATCH(self) -> None:  # noqa: N802
            self._route("PATCH")

        def _route(self, method: str) -> None:
            try:
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                if path == "/api/health" and method == "GET":
                    return self._send(200, {"status": "ok"})

                svc = state.service
                if path == "/api/spaces" and method == "POST":
                    return self._send(201, svc.create_space(self._read_json()))
                if path == "/api/spaces" and method == "GET":
                    return self._send(200, svc.list_spaces())
                if path == "/api/space-tree" and method == "GET":
                    return self._send(200, svc.space_tree())

                if path == "/api/layouts" and method == "POST":
                    return self._send(201, svc.create_layout(self._read_json()))
                if path == "/api/layouts/activate" and method == "POST":
                    return self._send(200, svc.activate_layout(self._read_json()))
                if path == "/api/closures" and method == "POST":
                    return self._send(201, svc.add_closure(self._read_json()))

                if path == "/api/events" and method == "POST":
                    return self._send(201, svc.create_event(self._read_json()))
                if path == "/api/events" and method == "GET":
                    return self._send(200, svc.list_events())
                if path == "/api/availability" and method == "POST":
                    return self._send(200, svc.check_availability(self._read_json()))

                if path.startswith("/api/spaces/") and method == "GET":
                    return self._send(200, svc.get_space(path.rsplit("/", 1)[1]))
                if path.startswith("/api/events/"):
                    return self._event_action(method, path)

                return self._send(404, {"error": "not_found",
                                        "message": f"无此路由：{method} {path}"})
            except VenueError as exc:
                self._send(exc.status, exc.to_dict())
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": "invalid", "message": str(exc)})
            except Exception as exc:  # 最后防线
                self._send(500, {"error": "internal", "message": repr(exc)})

        def _event_action(self, method: str, path: str) -> None:
            svc = state.service
            parts = path.split("/")
            event_id = parts[3]
            if len(parts) == 4 and method == "GET":
                return self._send(200, svc.get_event(event_id))
            if len(parts) == 5 and method == "POST":
                action = parts[4]
                body = self._read_json()
                if action == "submit":
                    return self._send(200, svc.submit_event(event_id, body or None))
                if action == "confirm":
                    version = body.get("expected_version")
                    return self._send(200, svc.confirm_event(event_id, version))
                if action == "start":
                    return self._send(200, svc.transition_event(event_id, "start"))
                if action == "settle":
                    return self._send(200, svc.transition_event(event_id, "settle"))
                if action == "cancel":
                    return self._send(200, svc.cancel_event(event_id))
            if len(parts) == 4 and method == "PATCH":
                return self._send(200, svc.update_event(event_id, self._read_json()))
            self._send(404, {"error": "not_found", "message": "无此活动操作"})

    return VenueHandler


def build_server(host: str = "127.0.0.1", port: int = 8000,
                 service: VenueService | None = None,
                 *, quiet: bool = False) -> ThreadingHTTPServer:
    state = ApiState(service)
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    server.quiet = quiet
    return server


def serve(host: str = "127.0.0.1", port: int = 8000,
          service: VenueService | None = None) -> None:  # pragma: no cover
    server = build_server(host, port, service)
    print(f"场地排期服务监听中：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
