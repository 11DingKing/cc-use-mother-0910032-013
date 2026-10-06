"""手动冒烟测试脚本（非自动化测试），验证完整业务链路。"""
from __future__ import annotations

import http.client
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(conn, method, path, body=None):
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode())
    return resp.status, data


def main() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "venue_scheduling.serve", "--port", "8765", "--seed"],
        cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.8)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=5)

        _, tree = call(conn, "GET", "/api/space-tree")
        print("1. 空间树:", tree[0]["name"],
              "->", [c["name"] for c in tree[0]["children"]])

        _, e1 = call(conn, "POST", "/api/events", {
            "name": "开幕式", "requested_space_id": "H1-MAIN",
            "requested_zone_id": "Z-whole",
            "start": "2026-10-10T09:00:00+00:00",
            "end": "2026-10-10T12:00:00+00:00",
            "attendees": 500, "required_equipment": ["舞台", "投影"]})
        call(conn, "POST", f"/api/events/{e1['event_id']}/submit", {})
        _, confirmed = call(conn, "POST", f"/api/events/{e1['event_id']}/confirm", {})
        print("2. 已排定:", confirmed["state"], "| 实际占用:",
              confirmed["snapshot"]["occ_start"], "->",
              confirmed["snapshot"]["occ_end"])

        _, e2 = call(conn, "POST", "/api/events", {
            "name": "校际交流", "requested_space_id": "H1-MAIN",
            "requested_zone_id": "Z-whole",
            "start": "2026-10-10T11:00:00+00:00",
            "end": "2026-10-10T13:00:00+00:00",
            "attendees": 200})
        call(conn, "POST", f"/api/events/{e2['event_id']}/submit", {})
        status, conflict = call(conn, "POST", f"/api/events/{e2['event_id']}/confirm", {})
        c = conflict["conflicts"][0]
        print(f"3. 同区域再订 -> HTTP {status} {conflict['error']}")
        print("   关系:", c["relationship"],
              "| 路径:", " > ".join(p["name"] for p in c["request_path"]))
        print("   碰撞窗口:", c["window"]["start"], "->", c["window"]["end"])
        print("   替代空间:", [(a["zone_name"], a["capacity"])
                            for a in conflict["alternatives"]])

        status, blocked = call(conn, "POST", "/api/layouts/activate", {
            "layout_id": "L-split",
            "effective_at": "2026-10-10T12:30:00+00:00",
            "setup_minutes": 60})
        print("4. 缓冲期内切布局 ->", status, blocked["error"],
              "| 阻挡活动:", blocked["conflicts"][0]["event_name"])

        status, switched = call(conn, "POST", "/api/layouts/activate", {
            "layout_id": "L-split",
            "effective_at": "2026-10-11T08:00:00+00:00",
            "setup_minutes": 60})
        print("5. 次日切换布局:", status, switched["activated"]["layout_id"])

        _, e3 = call(conn, "POST", "/api/events", {
            "name": "科学课", "requested_space_id": "H1-E",
            "requested_zone_id": "Z-east",
            "start": "2026-10-11T09:00:00+00:00",
            "end": "2026-10-11T11:00:00+00:00",
            "attendees": 280})
        call(conn, "POST", f"/api/events/{e3['event_id']}/submit", {})
        _, c3 = call(conn, "POST", f"/api/events/{e3['event_id']}/confirm", {})
        print("6. 拆分布局订东区:", c3["state"], "| 绑定布局:", c3["layout_id"],
              "| 空间链:", c3["snapshot"]["space_chain"])

        # 已订东区后尝试在同时间订整厅祖先（模拟切回全开）
        status, sw2 = call(conn, "POST", "/api/layouts/activate", {
            "layout_id": "L-open",
            "effective_at": "2026-10-11T10:00:00+00:00",
            "setup_minutes": 0})
        print("7. 占用期间切回全开 ->", status, sw2["error"],
              "|", sw2["conflicts"][0]["relationship"],
              sw2["conflicts"][0]["zone_name"])

        # 完成结算，保留原布局
        for path in ("start", "settle"):
            call(conn, "POST", f"/api/events/{e3['event_id']}/{path}", {})
        _, detail = call(conn, "GET", f"/api/events/{e3['event_id']}")
        print("8. 已结算:", detail["state"], "| 保留布局:", detail["layout_id"],
              "| 快照仍在:", detail["snapshot"] is not None)
        print("全部冒烟链路通过 ✓")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    main()
