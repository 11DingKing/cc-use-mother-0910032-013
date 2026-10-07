"""基于标准库 http.server 的 JSON HTTP 接口。

时间统一使用 ISO 8601（建议带时区，如 2026-11-02T09:00:00+08:00），
跨日活动直接以前后两个绝对时间表示。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .demo import build_demo_service
from .errors import ConflictError, NotFoundError, StateError, ValidationError
from .models import ActivityRequirement
from .service import VenueService


def _parse_time(value, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是 ISO 8601 时间") from None


def _need(body: dict, key: str):
    value = body.get(key)
    if value is None or value == "":
        raise ValidationError(f"缺少字段：{key}")
    return value


def _need_query(query: dict, key: str) -> str:
    values = query.get(key)
    if not values:
        raise ValidationError(f"缺少查询参数：{key}")
    return values[0]


def _optional_time(query: dict, key: str) -> datetime | None:
    values = query.get(key)
    return _parse_time(values[0], key) if values else None


class VenueAPI:
    """把 HTTP 请求路由到 VenueService，并映射为统一的状态码与错误体。"""

    def __init__(self, service: VenueService) -> None:
        self.service = service

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        try:
            return self._route(method, path, query, body)
        except ConflictError as exc:
            return 409, exc.to_dict()
        except NotFoundError as exc:
            return 404, {"error": "not_found", "message": str(exc)}
        except StateError as exc:
            return 409, {"error": "state", "message": str(exc)}
        except ValidationError as exc:
            return 400, {"error": "validation", "message": str(exc)}

    def _route(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        svc = self.service
        parts = [item for item in path.split("/") if item]

        if method == "GET" and not parts:
            return 200, {"service": "场地容量冲突治理", "status": "ok"}
        if method == "GET" and parts == ["spaces"]:
            return 200, {"spaces": svc.space_views()}
        if method == "POST" and parts == ["spaces"]:
            space = svc.add_space(
                _need(body, "space_id"), _need(body, "name"), body.get("parent_id")
            )
            return 201, svc.space_view(space.space_id)
        if method == "GET" and len(parts) == 2 and parts[0] == "spaces":
            return 200, svc.space_view(parts[1])
        if method == "GET" and len(parts) == 3 and parts[0] == "spaces" and parts[2] == "occupancy":
            return 200, svc.space_occupancy(
                parts[1], _optional_time(query, "start"), _optional_time(query, "end")
            )

        if method == "POST" and parts == ["layouts"]:
            layout = svc.add_layout(
                _need(body, "layout_id"),
                _need(body, "space_id"),
                _need(body, "name"),
                int(_need(body, "capacity")),
                body.get("equipment", []),
                int(body.get("conversion_minutes", 0)),
            )
            return 201, svc.layout_view(layout.layout_id)
        if method == "GET" and len(parts) == 2 and parts[0] == "layouts":
            return 200, svc.layout_view(parts[1])
        if method == "POST" and len(parts) == 3 and parts[0] == "layouts" and parts[2] == "versions":
            layout = svc.update_layout(
                parts[1],
                name=body.get("name"),
                capacity=body.get("capacity"),
                equipment=body.get("equipment"),
                conversion_minutes=body.get("conversion_minutes"),
            )
            return 201, svc.layout_view(layout.layout_id, layout.version)

        if method == "POST" and parts == ["closures"]:
            closure = svc.add_closure(
                _need(body, "closure_id"),
                _need(body, "space_id"),
                _parse_time(_need(body, "start"), "start"),
                _parse_time(_need(body, "end"), "end"),
                body.get("reason", ""),
            )
            return 201, svc.closure_view(closure.closure_id)
        if method == "DELETE" and len(parts) == 2 and parts[0] == "closures":
            svc.remove_closure(parts[1])
            return 200, {"removed": parts[1]}

        if method == "POST" and parts == ["bookings"]:
            requirement_body = body.get("requirement") or {}
            requirement = ActivityRequirement(
                capacity=int(requirement_body.get("capacity", 1)),
                equipment=frozenset(requirement_body.get("equipment", [])),
            )
            booking = svc.create_booking(
                _need(body, "booking_id"),
                _need(body, "activity_name"),
                _need(body, "space_id"),
                _need(body, "layout_id"),
                _parse_time(_need(body, "start"), "start"),
                _parse_time(_need(body, "end"), "end"),
                requirement,
            )
            if body.get("submit"):
                svc.submit(booking.booking_id)
            return 201, svc.booking_view(booking.booking_id)
        if method == "GET" and len(parts) == 2 and parts[0] == "bookings":
            return 200, svc.booking_view(parts[1])
        if method == "POST" and len(parts) == 3 and parts[0] == "bookings":
            actions = {
                "submit": svc.submit,
                "confirm": svc.confirm,
                "start": svc.start,
                "settle": svc.settle,
                "cancel": svc.cancel,
            }
            action = actions.get(parts[2])
            if action is None:
                raise NotFoundError(f"未知操作：{parts[2]}")
            action(parts[1])
            return 200, svc.booking_view(parts[1])

        if method == "GET" and parts == ["alternatives"]:
            equipment = query.get("equipment", [""])[0]
            requirement = ActivityRequirement(
                capacity=int(_need_query(query, "capacity")),
                equipment=frozenset(item for item in equipment.split(",") if item),
            )
            alternatives = svc.find_alternatives(
                requirement,
                _parse_time(_need_query(query, "start"), "start"),
                _parse_time(_need_query(query, "end"), "end"),
            )
            return 200, {"alternatives": alternatives}

        raise NotFoundError(f"未知路径：{method} {path}")


def make_handler(api: VenueAPI):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = None
            if body is not None and not isinstance(body, dict):
                body = None
            if body is None:
                status, payload = 400, {"error": "validation", "message": "请求体必须是 JSON 对象"}
            else:
                parsed = urlparse(self.path)
                status, payload = api.handle(method, parsed.path, parse_qs(parsed.query), body)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

    return Handler


def create_server(service: VenueService, host: str = "127.0.0.1", port: int = 8080):
    server = ThreadingHTTPServer((host, port), make_handler(VenueAPI(service)))
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="场地容量冲突治理 API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = create_server(build_demo_service(), args.host, args.port)
    print(f"场地容量冲突治理 API 已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
