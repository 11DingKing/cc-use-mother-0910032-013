"""场地排期核心行为测试。"""
from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_scheduling.errors import ConflictError, StateError, ValidationError
from venue_scheduling.seed import build_demo_service

UTC = timezone.utc
DAY1 = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
DAY2 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


class VenueTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_demo_service()

    def make_event(self, zone: str, start: datetime, end: datetime,
                   attendees: int = 100, equipment=None,
                   space: str = "H1-MAIN", **kw) -> str:
        result = self.svc.create_event({
            "name": f"活动-{zone}",
            "requested_space_id": space,
            "requested_zone_id": zone,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "attendees": attendees,
            "required_equipment": equipment or [],
            **kw,
        })
        return result["event_id"]

    def submit_confirm(self, event_id: str, data=None) -> dict:
        self.svc.submit_event(event_id, data)
        return self.svc.confirm_event(event_id)

    def activate_split(self, effective_at: datetime, setup: int = 120) -> dict:
        return self.svc.activate_layout({
            "layout_id": "L-split",
            "effective_at": effective_at.isoformat(),
            "setup_minutes": setup,
        })


class HierarchyConflictTest(VenueTestBase):
    def test_overlapping_same_zone_conflicts(self) -> None:
        self.submit_confirm(self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2)))
        e2 = self.make_event("Z-whole", DAY1 + timedelta(hours=1),
                             DAY1 + timedelta(hours=3))
        self.svc.submit_event(e2)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_event(e2)
        conflict = ctx.exception.payload["conflicts"][0]
        self.assertEqual(conflict["type"], "event")
        self.assertEqual(conflict["relationship"], "同区域占用")
        # 冲突路径：祖先链
        names = [p["name"] for p in conflict["request_path"]]
        self.assertEqual(names, ["科技馆", "1号馆", "中央展厅"])

    def test_buffer_expansion_blocks_adjacent_booking(self) -> None:
        # 9:00-11:00 占整厅（30 分钟缓冲 → 8:30-11:30）
        self.submit_confirm(self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2)))
        # 11:00-12:00 看似首尾相接，实际撞上缓冲
        e = self.make_event("Z-whole", DAY1 + timedelta(hours=2),
                            DAY1 + timedelta(hours=3))
        self.svc.submit_event(e)
        with self.assertRaises(ConflictError):
            self.svc.confirm_event(e)
        # 12:00 开始（缓冲外）可以
        ok = self.make_event("Z-whole", DAY1 + timedelta(hours=3),
                             DAY1 + timedelta(hours=4))
        self.submit_confirm(ok)

    def test_ancestor_descendant_conflict_across_layouts(self) -> None:
        # 整厅在全开布局下 9:00-12:00 被占用
        self.submit_confirm(self.make_event(
            "Z-whole", DAY1, DAY1 + timedelta(hours=3), attendees=500))

        # 次日切到拆分布局（10-12 全天用于转换，窗口必须避开占用）
        self.activate_split(DAY1 + timedelta(hours=12), setup=180)

        # 拆分布局下订东区：当天晚些没问题
        e = self.make_event("Z-east", DAY1 + timedelta(hours=13),
                            DAY1 + timedelta(hours=15),
                            attendees=200, space="H1-E")
        self.submit_confirm(e)

        # 再按全开布局订"整厅"不可能（布局已切换），但反过来：
        # 重新切回全开布局，整厅与东区活动构成祖先/子区域冲突
        self.svc.activate_layout({
            "layout_id": "L-open",
            "effective_at": (DAY1 + timedelta(hours=16)).isoformat(),
            "setup_minutes": 0,
        })
        whole = self.make_event("Z-whole", DAY1 + timedelta(hours=13, minutes=30),
                                DAY1 + timedelta(hours=14, minutes=30))
        self.svc.submit_event(whole)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_event(whole)
        rel = {c["relationship"] for c in ctx.exception.payload["conflicts"]}
        self.assertIn("子区域已被占用", rel)

    def test_descendant_blocking_ancestor_same_layout_tree(self) -> None:
        # 直接切拆分布局，先订东西区，再尝试订整厅（同物理树祖先）
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        e = self.make_event("Z-east", DAY1, DAY1 + timedelta(hours=2),
                            attendees=100, space="H1-E")
        self.submit_confirm(e)
        # 全开布局此时未生效，不能订 Z-whole；验证查询接口会给出不可订
        probe = self.svc.check_availability({
            "name": "探测",
            "requested_space_id": "H1-MAIN",
            "requested_zone_id": "Z-whole",
            "start": (DAY1 + timedelta(minutes=30)).isoformat(),
            "end": (DAY1 + timedelta(hours=1)).isoformat(),
            "attendees": 100,
        })
        self.assertFalse(probe["available"])

    def test_no_conflict_when_disjoint_spaces_or_times(self) -> None:
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        east = self.make_event("Z-east", DAY1, DAY1 + timedelta(hours=2),
                               attendees=100, space="H1-E")
        self.submit_confirm(east)
        # 同时段西区不冲突（兄弟节点）
        west = self.make_event("Z-west", DAY1, DAY1 + timedelta(hours=2),
                               attendees=100, space="H1-W")
        self.submit_confirm(west)
        # 东区另一天不冲突
        other_day = self.make_event("Z-east", DAY2, DAY2 + timedelta(hours=2),
                                    attendees=100, space="H1-E")
        self.submit_confirm(other_day)


class CapacityEquipmentTest(VenueTestBase):
    def test_capacity_and_equipment_rejected(self) -> None:
        e = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=1),
                            attendees=900, equipment=["同传", "直播台"])
        with self.assertRaises(ValidationError) as ctx:
            self.svc.submit_event(e)
        # 容量不足
        self.assertTrue(any("容量不足" in p for p in ctx.exception.payload["unmet"]))
        # 缺设备
        self.assertTrue(any("直播台" in p for p in ctx.exception.payload["unmet"]))

    def test_capacity_differs_per_layout_version(self) -> None:
        # 全开 600
        self.assertEqual(
            self.svc.get_space("H1-MAIN")["layouts"][0]["zones"][0]["capacity"], 600)
        # 拆分后东区只有 300
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        e = self.make_event("Z-east", DAY1, DAY1 + timedelta(hours=1),
                            attendees=400, space="H1-E")
        with self.assertRaises(ValidationError):
            self.svc.submit_event(e)


class CrossDayAndClosureTest(VenueTestBase):
    def test_cross_day_event_is_single_interval(self) -> None:
        # 跨日 10 日 20:00 -> 11 日 02:00
        e = self.make_event("Z-whole",
                            DAY1 + timedelta(hours=11),
                            DAY1 + timedelta(hours=17),
                            attendees=100)
        result = self.submit_confirm(e)
        self.assertEqual(result["snapshot"]["req_start"],
                         (DAY1 + timedelta(hours=11)).isoformat())
        # 11 日 01:00 的活动仍在跨日区间内 → 冲突
        other = self.make_event("Z-whole",
                                DAY1 + timedelta(hours=16),
                                DAY1 + timedelta(hours=18))
        self.svc.submit_event(other)
        with self.assertRaises(ConflictError):
            self.svc.confirm_event(other)

    def test_partial_closure_blocks_only_subtree(self) -> None:
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        east = self.make_event("Z-east", DAY1, DAY1 + timedelta(hours=2),
                               attendees=100, space="H1-E")
        self.submit_confirm(east)
        with self.assertRaises(ConflictError):
            self.svc.add_closure({
                "space_id": "H1-MAIN",
                "space_ids": ["H1-E"],
                "start": (DAY1 + timedelta(hours=1)).isoformat(),
                "end": (DAY1 + timedelta(hours=3)).isoformat(),
                "reason": "东区隔断维修",
            })
        # 同时段只关西区可以
        self.svc.add_closure({
            "space_id": "H1-MAIN",
            "space_ids": ["H1-W"],
            "start": (DAY1 + timedelta(hours=1)).isoformat(),
            "end": (DAY1 + timedelta(hours=3)).isoformat(),
        })
        # 既有活动占用到 11:30（含缓冲）；12:00-13:00 关闭东区（无占用，可登记）
        self.svc.add_closure({
            "space_id": "H1-MAIN",
            "space_ids": ["H1-E"],
            "start": (DAY1 + timedelta(hours=3)).isoformat(),
            "end": (DAY1 + timedelta(hours=4)).isoformat(),
            "reason": "东区隔断维修",
        })
        # 12:00-12:30 新预约（缓冲后 11:30 起，恰与既有活动占用结束时刻
        # 11:30 相接、左闭右开不冲突），但落在 12:00-13:00 关闭区间内 → 仅关闭冲突
        blocked = self.make_event("Z-east", DAY1 + timedelta(hours=3),
                                  DAY1 + timedelta(hours=3, minutes=30),
                                  attendees=10, space="H1-E")
        self.svc.submit_event(blocked)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_event(blocked)
        types = {c["type"] for c in ctx.exception.payload["conflicts"]}
        self.assertEqual(types, {"closure"})

    def test_full_closure_descendants_conflict(self) -> None:
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        self.svc.add_closure({
            "space_id": "H1-MAIN",
            "start": DAY1.isoformat(),
            "end": (DAY1 + timedelta(hours=4)).isoformat(),
            "reason": "消防演练",
        })
        for zone, sid in (("Z-east", "H1-E"), ("Z-west", "H1-W")):
            e = self.make_event(zone, DAY1 + timedelta(minutes=30),
                                DAY1 + timedelta(hours=1),
                                attendees=10, space=sid)
            self.svc.submit_event(e)
            with self.assertRaises(ConflictError):
                self.svc.confirm_event(e)


class LayoutSwitchTest(VenueTestBase):
    def test_switch_blocked_during_conversion_window(self) -> None:
        self.submit_confirm(self.make_event(
            "Z-whole", DAY1, DAY1 + timedelta(hours=3), attendees=100))
        with self.assertRaises(ConflictError) as ctx:
            # 转换窗口 10:00-12:00，与 9-12 点占用重叠
            self.activate_split(DAY1 + timedelta(hours=3), setup=120)
        conflict = ctx.exception.payload["conflicts"][0]
        self.assertEqual(conflict["event_name"], "活动-Z-whole")
        self.assertIn("conversion_window", ctx.exception.payload)

    def test_switch_succeeds_after_occupancy_plus_buffer(self) -> None:
        self.submit_confirm(self.make_event(
            "Z-whole", DAY1, DAY1 + timedelta(hours=2), attendees=100))
        # 占用实际到 11:30（含缓冲），转换窗口 12:00-14:00 避开后可行
        result = self.activate_split(DAY1 + timedelta(hours=5), setup=120)
        self.assertEqual(result["activated"]["layout_id"], "L-split")
        self.assertTrue(result["activated"]["active"])

    def test_finished_event_keeps_layout_and_allows_switch(self) -> None:
        e = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2))
        self.svc.submit_event(e)
        self.svc.confirm_event(e)
        self.svc.transition_event(e, "start")
        self.svc.transition_event(e, "settle")
        finished = self.svc.get_event(e)
        self.assertEqual(finished["state"], "已结算")
        self.assertEqual(finished["layout_id"], "L-open")
        self.assertIsNotNone(finished["snapshot"])
        # 已结算不阻挡布局切换（即便时间窗口与其快照重叠）
        result = self.svc.activate_layout({
            "layout_id": "L-split",
            "effective_at": (DAY1 + timedelta(hours=1)).isoformat(),
            "setup_minutes": 0,
        })
        self.assertEqual(result["activated"]["layout_id"], "L-split")


class ConcurrencyTest(VenueTestBase):
    def test_concurrent_confirm_only_one_wins(self) -> None:
        ids = [
            self.make_event("Z-whole", DAY1 + timedelta(minutes=i * 10),
                            DAY1 + timedelta(hours=2),
                            attendees=50)
            for i in range(8)
        ]
        # 全部进入待确认（软占用互不阻挡）
        for eid in ids:
            self.svc.submit_event(eid)

        outcomes: list[str] = []
        lock_results: list[str] = []

        def confirm(eid: str) -> None:
            try:
                self.svc.confirm_event(eid)
                lock_results.append("ok")
            except ConflictError:
                lock_results.append("conflict")
            except StateError:
                lock_results.append("state")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(confirm, ids))
        self.assertEqual(lock_results.count("ok"), 1)
        self.assertEqual(lock_results.count("conflict"), 7)

    def test_optimistic_version_cas(self) -> None:
        e = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=1))
        self.svc.submit_event(e)
        stale = self.svc.get_event(e)["version"]
        # 用过期版本确认 → 409
        with self.assertRaises(ConflictError):
            self.svc.confirm_event(e, expected_version=stale + 999)
        # 正确版本成功
        self.svc.confirm_event(e, expected_version=stale)


class AlternativesTest(VenueTestBase):
    def test_conflict_response_contains_alternatives(self) -> None:
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        # 占满东区 9-12
        east = self.make_event("Z-east", DAY1, DAY1 + timedelta(hours=3),
                               attendees=250, space="H1-E")
        self.submit_confirm(east)
        # 再订东区 250 人：容量够但冲突 → 应推荐同时段可用的西区（容量 260）
        e2 = self.make_event("Z-east", DAY1 + timedelta(minutes=30),
                             DAY1 + timedelta(hours=1),
                             attendees=250, space="H1-E")
        self.svc.submit_event(e2)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm_event(e2)
        alts = ctx.exception.payload["alternatives"]
        self.assertTrue(any(a["zone_id"] == "Z-west" for a in alts), alts)
        west = next(a for a in alts if a["zone_id"] == "Z-west")
        self.assertEqual(west["capacity"], 260)
        self.assertIn("path", west)

    def test_alternatives_respect_equipment_and_capacity(self) -> None:
        self.activate_split(DAY1 - timedelta(days=1), setup=0)
        # 需要同传：只有西区有；西区被占 → 没有替代
        west = self.make_event("Z-west", DAY1, DAY1 + timedelta(hours=2),
                               attendees=100, space="H1-W", equipment=["同传"])
        self.submit_confirm(west)
        probe = self.svc.check_availability({
            "name": "找同传厅",
            "requested_space_id": "H1-W",
            "requested_zone_id": "Z-west",
            "start": (DAY1 + timedelta(minutes=30)).isoformat(),
            "end": (DAY1 + timedelta(hours=1)).isoformat(),
            "attendees": 50,
            "required_equipment": ["同传"],
        })
        self.assertFalse(probe["available"])
        self.assertEqual(probe["alternatives"], [])


class SnapshotAndStateTest(VenueTestBase):
    def test_snapshot_expands_buffer_and_chain(self) -> None:
        e = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2))
        self.svc.submit_event(e)
        snap = self.svc.get_event(e)["snapshot"]
        self.assertEqual(snap["occ_start"],
                         (DAY1 - timedelta(minutes=30)).isoformat())
        self.assertEqual(snap["occ_end"],
                         (DAY1 + timedelta(hours=2, minutes=30)).isoformat())
        self.assertEqual(snap["space_chain"],
                         ["venue", "H1", "H1-MAIN"])
        self.assertEqual(snap["layout_id"], "L-open")

    def test_draft_editable_only_before_submit(self) -> None:
        e = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2))
        self.svc.update_event(e, {"attendees": 300})
        self.svc.submit_event(e)
        with self.assertRaises(StateError):
            self.svc.update_event(e, {"attendees": 1})

    def test_cancel_releases_occupancy(self) -> None:
        e1 = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2))
        self.submit_confirm(e1)
        e2 = self.make_event("Z-whole", DAY1, DAY1 + timedelta(hours=2))
        self.svc.submit_event(e2)
        with self.assertRaises(ConflictError):
            self.svc.confirm_event(e2)
        self.svc.cancel_event(e1)
        self.svc.confirm_event(e2)


if __name__ == "__main__":
    unittest.main()
