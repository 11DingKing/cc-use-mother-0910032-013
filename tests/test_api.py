"""HTTP API 端到端测试（标准库 http.client）。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_scheduling.api import build_server
from venue_scheduling.seed import build_demo_service

UTC = timezone.utc
DAY1 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = build_server(
            "127.0.0.1", 0, build_demo_service(), quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        data = json.loads(raw) if raw else None
        return resp.status, data

    def test_health_and_tree(self) -> None:
        status, data = self.call("GET", "/api/health")
        self.assertEqual((status, data["status"]), (200, "ok"))
        status, tree = self.call("GET", "/api/space-tree")
        self.assertEqual(status, 200)
        self.assertEqual(tree[0]["space_id"], "venue")
        main = tree[0]["children"][0]["children"][0]
        self.assertEqual(main["space_id"], "H1-MAIN")
        self.assertEqual(len(main["children"]), 2)

    def test_booking_conflict_returns_path_and_alternatives(self) -> None:
        body = {
            "name": "学校参观A",
            "requested_space_id": "H1-MAIN",
            "requested_zone_id": "Z-whole",
            "start": DAY1.isoformat(),
            "end": (DAY1 + timedelta(hours=2)).isoformat(),
            "attendees": 400,
            "required_equipment": ["投影"],
        }
        status, first = self.call("POST", "/api/events", body)
        self.assertEqual(status, 201)
        self.call("POST", f"/api/events/{first['event_id']}/submit", {})
        status, _ = self.call("POST", f"/api/events/{first['event_id']}/confirm", {})
        self.assertEqual(status, 200)

        status, second = self.call("POST", "/api/events", {
            **body, "name": "学校参观B",
            "start": (DAY1 + timedelta(hours=1)).isoformat(),
        })
        self.call("POST", f"/api/events/{second['event_id']}/submit", {})
        status, result = self.call(
            "POST", f"/api/events/{second['event_id']}/confirm", {})
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "conflict")
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["relationship"], "同区域占用")
        self.assertEqual([p["name"] for p in conflict["request_path"]],
                         ["科技馆", "1号馆", "中央展厅"])
        self.assertIn("window", conflict)
        self.assertIn("alternatives", result)

    def test_availability_probe_and_capacity_error(self) -> None:
        status, result = self.call("POST", "/api/availability", {
            "name": "探测",
            "requested_space_id": "H1-MAIN",
            "requested_zone_id": "Z-whole",
            "start": (DAY1 + timedelta(days=2)).isoformat(),
            "end": (DAY1 + timedelta(days=2, hours=1)).isoformat(),
            "attendees": 700,
        })
        self.assertEqual(status, 200)
        self.assertFalse(result["available"])
        self.assertTrue(any("容量不足" in p for p in result["unmet_requirements"]))

    def test_layout_switch_and_closure_via_http(self) -> None:
        # 切换到拆分布局（无占用窗口）
        status, result = self.call("POST", "/api/layouts/activate", {
            "layout_id": "L-split",
            "effective_at": (DAY1 + timedelta(days=10)).isoformat(),
            "setup_minutes": 60,
        })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["activated"]["active"], True)

        # 登记东区关闭
        status, closure = self.call("POST", "/api/closures", {
            "space_id": "H1-MAIN",
            "space_ids": ["H1-E"],
            "start": (DAY1 + timedelta(days=11)).isoformat(),
            "end": (DAY1 + timedelta(days=11, hours=2)).isoformat(),
            "reason": "维修",
        })
        self.assertEqual(status, 201, closure)
        self.assertIn("H1-E", closure["blocked_space_ids"])

        # 非法时间 → 400
        status, result = self.call("POST", "/api/closures", {
            "space_id": "H1-MAIN",
            "start": (DAY1 + timedelta(days=12)).isoformat(),
            "end": (DAY1 + timedelta(days=11)).isoformat(),
        })
        self.assertEqual(status, 400)

    def test_concurrent_confirm_over_http(self) -> None:
        # 先切换到拆分布局，再并发抢订西区
        status, _ = self.call("POST", "/api/layouts/activate", {
            "layout_id": "L-split",
            "effective_at": (DAY1 + timedelta(days=19)).isoformat(),
            "setup_minutes": 0,
        })
        self.assertEqual(status, 200)
        ids = []
        for i in range(5):
            _, event = self.call("POST", "/api/events", {
                "name": f"并发{i}",
                "requested_space_id": "H1-W",
                "requested_zone_id": "Z-west",
                "start": (DAY1 + timedelta(days=20, minutes=10 * i)).isoformat(),
                "end": (DAY1 + timedelta(days=20, hours=3)).isoformat(),
                "attendees": 50,
            })
            ids.append(event["event_id"])
        for eid in ids:
            self.call("POST", f"/api/events/{eid}/submit", {})

        def do_confirm(eid):
            status, _ = self.call("POST", f"/api/events/{eid}/confirm", {})
            return status

        with ThreadPoolExecutor(max_workers=5) as pool:
            statuses = list(pool.map(do_confirm, ids))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 4)

    def test_settled_event_keeps_layout(self) -> None:
        _, event = self.call("POST", "/api/events", {
            "name": "半日营",
            "requested_space_id": "H1-MAIN",
            "requested_zone_id": "Z-whole",
            "start": (DAY1 + timedelta(days=30)).isoformat(),
            "end": (DAY1 + timedelta(days=30, hours=2)).isoformat(),
            "attendees": 200,
        })
        eid = event["event_id"]
        for path in ("submit", "confirm", "start", "settle"):
            status, body = self.call("POST", f"/api/events/{eid}/{path}", {})
            self.assertEqual(status, 200, body)
        _, detail = self.call("GET", f"/api/events/{eid}")
        self.assertEqual(detail["state"], "已结算")
        self.assertEqual(detail["layout_id"], "L-open")
        self.assertIsNotNone(detail["snapshot"])
        # 已结算活动不阻挡同区域新布局切换
        status, result = self.call("POST", "/api/layouts/activate", {
            "layout_id": "L-split",
            "effective_at": (DAY1 + timedelta(days=30, hours=1)).isoformat(),
            "setup_minutes": 0,
        })
        self.assertEqual(status, 200, result)


if __name__ == "__main__":
    unittest.main()
