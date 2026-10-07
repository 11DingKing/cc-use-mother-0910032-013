"""核心服务测试：层级占用、布局缓冲、部分关闭、跨日、并发与历史布局。"""
from __future__ import annotations

import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_capacity.demo import build_demo_service
from venue_capacity.errors import ConflictError, StateError, ValidationError
from venue_capacity.models import ActivityRequirement, BookingState

CST = timezone(timedelta(hours=8))
DAY = datetime(2026, 11, 2, 0, 0, tzinfo=CST)


def at(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    return DAY + timedelta(days=day_offset, hours=hour, minutes=minute)


def req(capacity: int, equipment=()) -> ActivityRequirement:
    return ActivityRequirement(capacity=capacity, equipment=frozenset(equipment))


class VenueServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_demo_service()

    def book(self, booking_id, space_id, layout_id, start, end, requirement=None, submit=True):
        self.svc.create_booking(
            booking_id, f"活动{booking_id}", space_id, layout_id, start, end,
            requirement or req(50),
        )
        if submit:
            self.svc.submit(booking_id)
        return booking_id

    # ------------------------------------------------------------------
    # 祖先与子区域冲突
    # ------------------------------------------------------------------
    def test_parent_booking_blocks_child_space(self) -> None:
        self.book("A1", "hall", "hall-full", at(9), at(12))
        self.svc.confirm("A1")
        self.book("A2", "hall-east", "east-theatre", at(10), at(11))
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm("A2")
        conflict = ctx.exception.conflicts[0]
        self.assertEqual(conflict["path"], "市民文化中心/综合展厅")
        self.assertIn("市民文化中心/综合展厅/东区", conflict["shared_spaces"])

    def test_child_booking_blocks_parent_and_sibling_is_free(self) -> None:
        self.book("B1", "hall-east", "east-theatre", at(9), at(12))
        self.svc.confirm("B1")
        # 子区域已占用 -> 祖先（整厅）同时段不可约
        self.book("B2", "hall", "hall-full", at(9), at(12))
        with self.assertRaises(ConflictError):
            self.svc.confirm("B2")
        # 兄弟区域（西区）不受影响：隔断区域不会被重复使用，其余区域仍可排期
        self.book("B3", "hall-west", "west-theatre", at(9), at(12))
        self.svc.confirm("B3")
        view = self.svc.booking_view("B1")
        self.assertEqual(view["occupancy"]["leaves"], ["市民文化中心/综合展厅/东区"])
        self.assertIn("市民文化中心/综合展厅", view["occupancy"]["blocked_ancestors"])

    # ------------------------------------------------------------------
    # 布局转换缓冲
    # ------------------------------------------------------------------
    def test_conversion_buffer_between_different_layouts(self) -> None:
        self.book("C1", "hall-east", "east-theatre", at(9), at(10))
        self.svc.confirm("C1")
        # 宴会式需要 45 分钟转换，10:30 开始不足
        self.book("C2", "hall-east", "east-banquet", at(10, 30), at(12))
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm("C2")
        self.assertIn("缓冲", ctx.exception.conflicts[0]["reason"])
        # 10:45 开始满足缓冲
        self.book("C3", "hall-east", "east-banquet", at(10, 45), at(12))
        self.svc.confirm("C3")

    def test_buffer_applies_in_both_directions(self) -> None:
        self.book("D1", "hall-east", "east-banquet", at(11), at(12))
        self.svc.confirm("D1")
        # 新预约更早结束，但后到的宴会式仍需 45 分钟缓冲
        self.book("D2", "hall-east", "east-theatre", at(9), at(10, 30))
        with self.assertRaises(ConflictError):
            self.svc.confirm("D2")
        self.book("D3", "hall-east", "east-theatre", at(9), at(10, 15))
        self.svc.confirm("D3")

    def test_same_layout_needs_no_buffer(self) -> None:
        self.book("E1", "hall-east", "east-theatre", at(9), at(10))
        self.svc.confirm("E1")
        self.book("E2", "hall-east", "east-theatre", at(10), at(12))
        self.svc.confirm("E2")

    # ------------------------------------------------------------------
    # 部分关闭
    # ------------------------------------------------------------------
    def test_partial_closure_blocks_only_closed_area(self) -> None:
        self.svc.add_closure("CL1", "hall-east", at(13), at(15), reason="设备检修")
        self.book("F1", "hall-east", "east-theatre", at(14), at(16))
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm("F1")
        self.assertEqual(ctx.exception.conflicts[0]["type"], "closure")
        # 西区与多功能室不受东区关闭影响
        self.book("F2", "hall-west", "west-theatre", at(14), at(16))
        self.svc.confirm("F2")
        # 整厅包含被关闭的东区，同样不可约
        self.book("F3", "hall", "hall-full", at(14), at(16))
        with self.assertRaises(ConflictError):
            self.svc.confirm("F3")

    def test_closure_rejects_conflicting_booking_time(self) -> None:
        self.book("G1", "hall-east", "east-theatre", at(9), at(12))
        self.svc.confirm("G1")
        with self.assertRaises(ConflictError):
            self.svc.add_closure("CL2", "hall", at(10), at(11), reason="消防检查")

    # ------------------------------------------------------------------
    # 跨日活动
    # ------------------------------------------------------------------
    def test_cross_day_activity(self) -> None:
        self.book("H1", "hall-east", "east-theatre", at(22), at(2, day_offset=1))
        self.svc.confirm("H1")
        # 跨日时段内重叠仍冲突
        self.book("H2", "hall-east", "east-theatre", at(23), at(1, day_offset=1))
        with self.assertRaises(ConflictError):
            self.svc.confirm("H2")
        # 次日 02:00 同布局可无缝衔接
        self.book("H3", "hall-east", "east-theatre", at(2, day_offset=1), at(4, day_offset=1))
        self.svc.confirm("H3")
        # 次日 04:00 后换宴会式需 45 分钟缓冲：04:30 不足，04:45 可行
        self.book("H4", "hall-east", "east-banquet", at(4, 30, day_offset=1), at(6, day_offset=1))
        with self.assertRaises(ConflictError):
            self.svc.confirm("H4")
        self.book("H5", "hall-east", "east-banquet", at(4, 45, day_offset=1), at(6, day_offset=1))
        self.svc.confirm("H5")

    # ------------------------------------------------------------------
    # 并发确认原子更新
    # ------------------------------------------------------------------
    def test_concurrent_confirm_is_atomic(self) -> None:
        for round_no in range(20):
            svc = build_demo_service()
            for bid in ("P", "Q"):
                svc.create_booking(bid, f"活动{bid}", "hall-east", "east-theatre",
                                   at(9), at(12), req(50))
                svc.submit(bid)
            barrier = threading.Barrier(2)
            results: dict[str, str] = {}

            def worker(bid: str) -> None:
                barrier.wait()
                try:
                    svc.confirm(bid)
                    results[bid] = "ok"
                except ConflictError:
                    results[bid] = "conflict"

            threads = [threading.Thread(target=worker, args=(bid,)) for bid in ("P", "Q")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(
                sorted(results.values()), ["conflict", "ok"],
                f"第 {round_no} 轮并发确认结果异常：{results}",
            )

    def test_double_confirm_rejected(self) -> None:
        self.book("I1", "hall-east", "east-theatre", at(9), at(12))
        self.svc.confirm("I1")
        with self.assertRaises(StateError):
            self.svc.confirm("I1")

    # ------------------------------------------------------------------
    # 已完成活动保留原布局
    # ------------------------------------------------------------------
    def test_settled_booking_keeps_original_layout(self) -> None:
        self.book("J1", "hall-east", "east-theatre", at(9), at(12), req(120))
        self.svc.confirm("J1")
        self.svc.start("J1")
        self.svc.settle("J1")
        # 布局升级为新版本：容量与设备变化
        self.svc.update_layout("east-theatre", capacity=180, equipment={"projector"})
        view = self.svc.booking_view("J1")
        self.assertEqual(view["state"], BookingState.SETTLED.value)
        self.assertEqual(view["layout"]["version"], 1)
        self.assertEqual(view["layout"]["capacity"], 200)
        self.assertEqual(view["layout"]["equipment"], ["projector", "sound"])
        # 新预约钉住新版本
        self.book("J2", "hall-east", "east-theatre", at(14), at(16))
        self.assertEqual(self.svc.booking_view("J2")["layout_version"], 2)

    def test_submit_pins_layout_version_before_confirm(self) -> None:
        self.book("K1", "hall-east", "east-theatre", at(9), at(12))
        self.svc.update_layout("east-theatre", capacity=190)
        self.svc.confirm("K1")
        view = self.svc.booking_view("K1")
        self.assertEqual(view["layout"]["version"], 1)
        self.assertEqual(view["layout"]["capacity"], 200)

    # ------------------------------------------------------------------
    # 冲突路径与可行替代空间
    # ------------------------------------------------------------------
    def test_conflict_error_carries_alternatives(self) -> None:
        self.book("L1", "hall-east", "east-theatre", at(9), at(12))
        self.svc.confirm("L1")
        self.book("L2", "hall-east", "east-theatre", at(10), at(11), req(150, {"projector"}))
        with self.assertRaises(ConflictError) as ctx:
            self.svc.confirm("L2")
        error = ctx.exception
        self.assertEqual(error.conflicts[0]["path"], "市民文化中心/综合展厅/东区")
        paths = [item["path"] for item in error.alternatives]
        # 东区被占用时整厅也不可行；西区剧场式满足人数与设备，多功能室容量不足被排除
        self.assertEqual(paths, ["市民文化中心/综合展厅/西区"])
        self.assertEqual(error.alternatives[0]["capacity"], 180)
        # 空闲时段：西区（180）、东区（200）与整厅（500）都可行，按容量升序
        free = self.svc.find_alternatives(req(150, {"projector"}), at(14), at(16))
        self.assertEqual(
            [item["path"] for item in free],
            [
                "市民文化中心/综合展厅/西区",
                "市民文化中心/综合展厅/东区",
                "市民文化中心/综合展厅",
            ],
        )

    def test_alternatives_respect_equipment(self) -> None:
        options = self.svc.find_alternatives(req(50, {"booth"}), at(9), at(12))
        self.assertEqual([item["layout_id"] for item in options], ["west-expo"])

    # ------------------------------------------------------------------
    # 需求校验与取消释放
    # ------------------------------------------------------------------
    def test_submit_validates_requirement(self) -> None:
        self.book("M1", "hall-east", "east-theatre", at(9), at(12), req(250), submit=False)
        with self.assertRaises(ValidationError):
            self.svc.submit("M1")
        self.book("M2", "hall-east", "east-theatre", at(9), at(12), req(50, {"mic"}), submit=False)
        with self.assertRaises(ValidationError):
            self.svc.submit("M2")

    def test_cancel_frees_occupancy(self) -> None:
        self.book("N1", "hall-east", "east-theatre", at(9), at(12))
        self.svc.confirm("N1")
        self.svc.cancel("N1")
        self.book("N2", "hall-east", "east-theatre", at(9), at(12))
        self.svc.confirm("N2")
        with self.assertRaises(StateError):
            self.svc.cancel("N1")  # 已取消不可重复取消

    def test_layout_must_belong_to_space(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_booking(
                "O1", "错位布局", "hall-west", "east-theatre", at(9), at(12), req(50)
            )


if __name__ == "__main__":
    unittest.main()
