"""HTTP API 测试：完整预约流程与冲突响应结构。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_capacity.api import create_server
from venue_capacity.demo import build_demo_service


class VenueAPITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server(build_demo_service(), "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def call(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_booking_flow_and_occupancy(self) -> None:
        status, created = self.call("POST", "/bookings", {
            "booking_id": "API-1",
            "activity_name": "科普讲座",
            "space_id": "hall-east",
            "layout_id": "east-theatre",
            "start": "2026-11-03T09:00:00+08:00",
            "end": "2026-11-03T12:00:00+08:00",
            "requirement": {"capacity": 120, "equipment": ["projector"]},
            "submit": True,
        })
        self.assertEqual(status, 201)
        self.assertEqual(created["state"], "待确认")

        status, confirmed = self.call("POST", "/bookings/API-1/confirm")
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["state"], "已排定")
        self.assertEqual(confirmed["layout"]["name"], "剧场式")
        self.assertEqual(
            confirmed["occupancy"]["leaves"], ["市民文化中心/综合展厅/东区"]
        )

        status, occupancy = self.call(
            "GET",
            "/spaces/hall/occupancy?" + urlencode({
                "start": "2026-11-03T00:00:00+08:00",
                "end": "2026-11-04T00:00:00+08:00",
            }),
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["booking_id"] for item in occupancy["bookings"]], ["API-1"])

    def test_conflict_response_contains_paths_and_alternatives(self) -> None:
        for booking_id, start, end in (
            ("API-2", "2026-11-04T09:00:00+08:00", "2026-11-04T12:00:00+08:00"),
            ("API-3", "2026-11-04T10:00:00+08:00", "2026-11-04T11:00:00+08:00"),
        ):
            status, _ = self.call("POST", "/bookings", {
                "booking_id": booking_id,
                "activity_name": f"活动{booking_id}",
                "space_id": "hall-east",
                "layout_id": "east-theatre",
                "start": start,
                "end": end,
                "requirement": {"capacity": 100},
                "submit": True,
            })
            self.assertEqual(status, 201)
        status, _ = self.call("POST", "/bookings/API-2/confirm")
        self.assertEqual(status, 200)

        status, payload = self.call("POST", "/bookings/API-3/confirm")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "conflict")
        self.assertEqual(
            payload["conflicts"][0]["path"], "市民文化中心/综合展厅/东区"
        )
        alternative_paths = [item["path"] for item in payload["alternatives"]]
        self.assertIn("市民文化中心/综合展厅/西区", alternative_paths)

    def test_not_found_and_validation(self) -> None:
        status, payload = self.call("GET", "/bookings/missing")
        self.assertEqual(status, 404)
        status, payload = self.call("POST", "/bookings", {
            "booking_id": "API-4",
            "activity_name": "坏时间",
            "space_id": "hall-east",
            "layout_id": "east-theatre",
            "start": "不是时间",
            "end": "2026-11-04T12:00:00+08:00",
        })
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
