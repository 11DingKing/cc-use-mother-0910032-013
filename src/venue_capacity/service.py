"""核心服务：层级占用展开、冲突检测、转换缓冲与原子确认。

占用模型：每个预约在确认时展开为其空间子树下的全部叶子空间（实际物理区域），
写入叶子占用索引。两个预约的叶子集合相交即可能冲突：

- 时间重叠：同一物理区域不允许重叠使用（祖先与子区域天然互斥）；
- 转换缓冲：同一区域上先后两个预约布局不同（layout_id 不同，与版本无关），
  较晚开始的一方必须留出其布局的 conversion_minutes 转换时间。

关闭（Closure）按同样规则展开叶子，但与预约只做严格时间重叠判断，不涉及布局缓冲。
所有占用变更（确认、取消、关闭）都在同一把锁内完成，保证并发确认原子生效。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from .errors import ConflictError, NotFoundError, StateError, ValidationError
from .models import (
    OCCUPYING_STATES,
    ActivityRequirement,
    Booking,
    BookingState,
    Closure,
    Layout,
    Space,
)


def _time_conflict(
    start: datetime,
    end: datetime,
    layout: Layout,
    other_start: datetime,
    other_end: datetime,
    other_layout: Layout,
) -> tuple[bool, str]:
    """判断两段占用是否冲突，返回 (是否冲突, 原因)。"""
    if start < other_end and other_start < end:
        return True, "时间重叠"
    if layout.layout_id == other_layout.layout_id:
        return False, ""
    # 布局不同：较晚开始的一方需要留出自己布局的转换缓冲
    if start >= other_end:
        need = timedelta(minutes=layout.conversion_minutes)
        if start - other_end < need:
            return True, f"布局转换缓冲不足：需 {layout.conversion_minutes} 分钟"
        return False, ""
    need = timedelta(minutes=other_layout.conversion_minutes)
    if other_start - end < need:
        return True, f"布局转换缓冲不足：需 {other_layout.conversion_minutes} 分钟"
    return False, ""


class VenueService:
    """场馆预约领域服务，所有读写都在内存仓储上原子完成。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._spaces: dict[str, Space] = {}
        self._children: dict[str | None, list[str]] = {}
        self._layouts: dict[str, list[Layout]] = {}  # layout_id -> 按版本升序
        self._space_layouts: dict[str, list[str]] = {}  # space_id -> [layout_id]
        self._bookings: dict[str, Booking] = {}
        self._closures: dict[str, Closure] = {}
        self._occupancy: dict[str, set[str]] = {}  # 叶子空间 -> 占用中的 booking_id
        self._closure_leaves: dict[str, set[str]] = {}  # 叶子空间 -> closure_id
        self._leaf_cache: dict[str, frozenset[str]] = {}

    # ------------------------------------------------------------------
    # 空间与布局维护
    # ------------------------------------------------------------------
    def add_space(self, space_id: str, name: str, parent_id: str | None = None) -> Space:
        with self._lock:
            if not space_id:
                raise ValidationError("空间编号不能为空")
            if space_id in self._spaces:
                raise ValidationError(f"空间已存在：{space_id}")
            if parent_id is not None and parent_id not in self._spaces:
                raise NotFoundError(f"父空间不存在：{parent_id}")
            space = Space(space_id=space_id, name=name, parent_id=parent_id)
            self._spaces[space_id] = space
            self._children.setdefault(parent_id, []).append(space_id)
            self._children.setdefault(space_id, [])
            self._leaf_cache.clear()
            return space

    def add_layout(
        self,
        layout_id: str,
        space_id: str,
        name: str,
        capacity: int,
        equipment=(),
        conversion_minutes: int = 0,
    ) -> Layout:
        """为空间登记布局，初始版本为 1。"""
        with self._lock:
            self._require_space(space_id)
            if layout_id in self._layouts:
                raise ValidationError(f"布局已存在：{layout_id}")
            self._check_layout_params(capacity, conversion_minutes)
            layout = Layout(
                layout_id=layout_id,
                space_id=space_id,
                version=1,
                name=name,
                capacity=capacity,
                equipment=frozenset(equipment),
                conversion_minutes=conversion_minutes,
            )
            self._layouts[layout_id] = [layout]
            self._space_layouts.setdefault(space_id, []).append(layout_id)
            return layout

    def update_layout(
        self,
        layout_id: str,
        *,
        name: str | None = None,
        capacity: int | None = None,
        equipment=None,
        conversion_minutes: int | None = None,
    ) -> Layout:
        """修改布局：产生新的不可变版本，旧版本保留给已结算活动。"""
        with self._lock:
            versions = self._require_layouts(layout_id)
            current = versions[-1]
            new_capacity = current.capacity if capacity is None else capacity
            new_conversion = (
                current.conversion_minutes if conversion_minutes is None else conversion_minutes
            )
            self._check_layout_params(new_capacity, new_conversion)
            layout = Layout(
                layout_id=layout_id,
                space_id=current.space_id,
                version=current.version + 1,
                name=current.name if name is None else name,
                capacity=new_capacity,
                equipment=current.equipment if equipment is None else frozenset(equipment),
                conversion_minutes=new_conversion,
            )
            versions.append(layout)
            return layout

    def retire_layout(self, layout_id: str) -> Layout:
        """停用布局：新版本不再可约，历史预约不受影响。"""
        with self._lock:
            versions = self._require_layouts(layout_id)
            current = versions[-1]
            retired = Layout(
                layout_id=current.layout_id,
                space_id=current.space_id,
                version=current.version,
                name=current.name,
                capacity=current.capacity,
                equipment=current.equipment,
                conversion_minutes=current.conversion_minutes,
                retired=True,
            )
            versions[-1] = retired
            return retired

    def latest_layout(self, layout_id: str) -> Layout:
        """最新未停用版本。"""
        with self._lock:
            versions = self._require_layouts(layout_id)
            for layout in reversed(versions):
                if not layout.retired:
                    return layout
            raise NotFoundError(f"布局已停用：{layout_id}")

    # ------------------------------------------------------------------
    # 预约生命周期：筹备 -> 待确认 -> 已排定 -> 执行中 -> 已结算
    # ------------------------------------------------------------------
    def create_booking(
        self,
        booking_id: str,
        activity_name: str,
        space_id: str,
        layout_id: str,
        start: datetime,
        end: datetime,
        requirement: ActivityRequirement,
    ) -> Booking:
        """创建筹备中的预约；时间使用绝对时间，天然支持跨日活动。"""
        with self._lock:
            if not booking_id:
                raise ValidationError("预约编号不能为空")
            if booking_id in self._bookings:
                raise ValidationError(f"预约已存在：{booking_id}")
            self._require_space(space_id)
            layouts = self._require_layouts(layout_id)
            if layouts[0].space_id != space_id:
                raise ValidationError(f"布局 {layout_id} 不属于空间 {space_id}")
            if not end > start:
                raise ValidationError("结束时间必须晚于开始时间")
            if requirement.capacity <= 0:
                raise ValidationError("需求人数必须为正数")
            booking = Booking(
                booking_id=booking_id,
                activity_name=activity_name,
                space_id=space_id,
                layout_id=layout_id,
                start=start,
                end=end,
                requirement=requirement,
            )
            self._bookings[booking_id] = booking
            return booking

    def submit(self, booking_id: str) -> Booking:
        """提交预约：钉住最新布局版本并校验活动需求，进入待确认。"""
        with self._lock:
            booking = self._require_booking(booking_id)
            self._require_state(booking, BookingState.DRAFT, "提交")
            layout = self.latest_layout(booking.layout_id)
            if booking.requirement.capacity > layout.capacity:
                raise ValidationError(
                    f"需求人数 {booking.requirement.capacity} 超过布局容量 {layout.capacity}"
                )
            missing = booking.requirement.equipment - layout.equipment
            if missing:
                raise ValidationError("布局缺少设备：" + "、".join(sorted(missing)))
            booking.layout_version = layout.version
            booking.state = BookingState.PENDING
            return booking

    def confirm(self, booking_id: str) -> Booking:
        """确认预约：在同一把锁内检测冲突并原子写入占用。

        并发确认同一区域时只有一个成功，其余收到携带冲突路径与
        可行替代空间的 ConflictError。
        """
        with self._lock:
            booking = self._require_booking(booking_id)
            self._require_state(booking, BookingState.PENDING, "确认")
            if booking.layout_version is None:
                raise StateError("预约尚未提交")
            layout = self._layout_version(booking.layout_id, booking.layout_version)
            leaves = self._leaves(booking.space_id)
            conflicts = self._detect_conflicts(leaves, layout, booking.start, booking.end)
            if conflicts:
                raise ConflictError(
                    conflicts, self.find_alternatives(booking.requirement, booking.start, booking.end)
                )
            booking.state = BookingState.SCHEDULED
            booking.layout_snapshot = self._snapshot(layout)
            booking.occupied_leaves = leaves
            for leaf in leaves:
                self._occupancy.setdefault(leaf, set()).add(booking_id)
            return booking

    def start(self, booking_id: str) -> Booking:
        """活动开始执行。"""
        with self._lock:
            booking = self._require_booking(booking_id)
            self._require_state(booking, BookingState.SCHEDULED, "开始执行")
            booking.state = BookingState.IN_PROGRESS
            return booking

    def settle(self, booking_id: str) -> Booking:
        """结算活动：保留确认时的布局快照，历史占用仍可追溯。"""
        with self._lock:
            booking = self._require_booking(booking_id)
            self._require_state(booking, BookingState.IN_PROGRESS, "结算")
            booking.state = BookingState.SETTLED
            return booking

    def cancel(self, booking_id: str) -> Booking:
        """取消预约并原子释放占用；执行中与已结算的活动不可取消。"""
        with self._lock:
            booking = self._require_booking(booking_id)
            allowed = {BookingState.DRAFT, BookingState.PENDING, BookingState.SCHEDULED}
            if booking.state not in allowed:
                raise StateError(f"当前状态不允许取消：{booking.state.value}")
            if booking.state is BookingState.SCHEDULED:
                self._release_occupancy(booking)
            booking.state = BookingState.CANCELLED
            return booking

    # ------------------------------------------------------------------
    # 部分关闭
    # ------------------------------------------------------------------
    def add_closure(
        self, closure_id: str, space_id: str, start: datetime, end: datetime, reason: str = ""
    ) -> Closure:
        """关闭空间（可只关闭展厅的某个子区域）；与已占用预约严格冲突。"""
        with self._lock:
            if not closure_id:
                raise ValidationError("关闭编号不能为空")
            if closure_id in self._closures:
                raise ValidationError(f"关闭记录已存在：{closure_id}")
            self._require_space(space_id)
            if not end > start:
                raise ValidationError("结束时间必须晚于开始时间")
            leaves = self._leaves(space_id)
            conflicts: list[dict] = []
            seen: set[str] = set()
            for leaf in leaves:
                for other_id in self._occupancy.get(leaf, ()):
                    if other_id in seen:
                        continue
                    seen.add(other_id)
                    other = self._bookings[other_id]
                    if start < other.end and other.start < end:
                        conflicts.append(self._booking_conflict(other, leaves, "已有预约占用"))
            if conflicts:
                raise ConflictError(conflicts)
            closure = Closure(
                closure_id=closure_id, space_id=space_id, start=start, end=end, reason=reason
            )
            self._closures[closure_id] = closure
            for leaf in leaves:
                self._closure_leaves.setdefault(leaf, set()).add(closure_id)
            return closure

    def remove_closure(self, closure_id: str) -> None:
        with self._lock:
            closure = self._closures.pop(closure_id, None)
            if closure is None:
                raise NotFoundError(f"关闭记录不存在：{closure_id}")
            for leaf in self._leaves(closure.space_id):
                ids = self._closure_leaves.get(leaf)
                if ids:
                    ids.discard(closure_id)

    # ------------------------------------------------------------------
    # 冲突检测与替代空间
    # ------------------------------------------------------------------
    def find_alternatives(
        self, requirement: ActivityRequirement, start: datetime, end: datetime, limit: int = 20
    ) -> list[dict]:
        """查找同时段可行替代空间：容量、设备满足且无冲突，按容量升序（就近适配）。"""
        with self._lock:
            options: list[dict] = []
            for space_id in sorted(self._spaces, key=self.path_of):
                for layout_id in self._space_layouts.get(space_id, []):
                    try:
                        layout = self.latest_layout(layout_id)
                    except NotFoundError:
                        continue
                    if layout.capacity < requirement.capacity:
                        continue
                    if not requirement.equipment <= layout.equipment:
                        continue
                    if self._detect_conflicts(self._leaves(space_id), layout, start, end):
                        continue
                    options.append(
                        {
                            "space_id": space_id,
                            "path": self.path_of(space_id),
                            "layout_id": layout.layout_id,
                            "layout_version": layout.version,
                            "layout_name": layout.name,
                            "capacity": layout.capacity,
                            "equipment": sorted(layout.equipment),
                            "conversion_minutes": layout.conversion_minutes,
                        }
                    )
            options.sort(key=lambda item: (item["capacity"], item["path"]))
            return options[:limit]

    def _detect_conflicts(
        self,
        leaves: frozenset[str],
        layout: Layout,
        start: datetime,
        end: datetime,
        exclude_booking: str | None = None,
    ) -> list[dict]:
        """对展开的叶子集合检测预约与关闭冲突。"""
        conflicts: list[dict] = []
        seen_bookings: set[str] = set()
        seen_closures: set[str] = set()
        for leaf in leaves:
            for other_id in self._occupancy.get(leaf, ()):
                if other_id == exclude_booking or other_id in seen_bookings:
                    continue
                seen_bookings.add(other_id)
                other = self._bookings[other_id]
                other_layout = self._layout_version(other.layout_id, other.layout_version)
                hit, reason = _time_conflict(
                    start, end, layout, other.start, other.end, other_layout
                )
                if hit:
                    conflicts.append(self._booking_conflict(other, leaves, reason))
            for closure_id in self._closure_leaves.get(leaf, ()):
                if closure_id in seen_closures:
                    continue
                seen_closures.add(closure_id)
                closure = self._closures[closure_id]
                if start < closure.end and closure.start < end:
                    shared = sorted(leaves & self._leaves(closure.space_id))
                    conflicts.append(
                        {
                            "type": "closure",
                            "closure_id": closure.closure_id,
                            "space_id": closure.space_id,
                            "path": self.path_of(closure.space_id),
                            "reason": f"空间关闭：{closure.reason}" if closure.reason else "空间关闭",
                            "start": closure.start.isoformat(),
                            "end": closure.end.isoformat(),
                            "shared_spaces": [self.path_of(item) for item in shared],
                        }
                    )
        return conflicts

    def _booking_conflict(self, other: Booking, leaves: frozenset[str], reason: str) -> dict:
        shared = sorted(leaves & (other.occupied_leaves or frozenset()))
        return {
            "type": "booking",
            "booking_id": other.booking_id,
            "activity_name": other.activity_name,
            "space_id": other.space_id,
            "path": self.path_of(other.space_id),
            "state": other.state.value,
            "reason": reason,
            "start": other.start.isoformat(),
            "end": other.end.isoformat(),
            "shared_spaces": [self.path_of(item) for item in shared],
        }

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------
    def booking_view(self, booking_id: str) -> dict:
        with self._lock:
            booking = self._require_booking(booking_id)
            occupying = booking.state in OCCUPYING_STATES and booking.occupied_leaves
            return {
                "booking_id": booking.booking_id,
                "activity_name": booking.activity_name,
                "state": booking.state.value,
                "space_id": booking.space_id,
                "path": self.path_of(booking.space_id),
                "layout_id": booking.layout_id,
                "layout_version": booking.layout_version,
                "layout": booking.layout_snapshot,
                "start": booking.start.isoformat(),
                "end": booking.end.isoformat(),
                "requirement": {
                    "capacity": booking.requirement.capacity,
                    "equipment": sorted(booking.requirement.equipment),
                },
                "occupancy": {
                    "leaves": sorted(self.path_of(item) for item in booking.occupied_leaves or ()),
                    "blocked_ancestors": (
                        [self.path_of(item) for item in self._ancestors_of(booking.space_id)]
                        if occupying
                        else []
                    ),
                },
            }

    def space_view(self, space_id: str) -> dict:
        with self._lock:
            space = self._require_space(space_id)
            layouts = []
            for layout_id in self._space_layouts.get(space_id, []):
                try:
                    layouts.append(self._snapshot(self.latest_layout(layout_id)))
                except NotFoundError:
                    continue
            return {
                "space_id": space.space_id,
                "name": space.name,
                "parent_id": space.parent_id,
                "path": self.path_of(space_id),
                "leaves": sorted(self.path_of(item) for item in self._leaves(space_id)),
                "layouts": layouts,
            }

    def space_views(self) -> list[dict]:
        with self._lock:
            return [self.space_view(sid) for sid in sorted(self._spaces, key=self.path_of)]

    def layout_view(self, layout_id: str, version: int | None = None) -> dict:
        with self._lock:
            layout = (
                self.latest_layout(layout_id)
                if version is None
                else self._layout_version(layout_id, version)
            )
            view = self._snapshot(layout)
            view["retired"] = layout.retired
            return view

    def closure_view(self, closure_id: str) -> dict:
        with self._lock:
            closure = self._closures.get(closure_id)
            if closure is None:
                raise NotFoundError(f"关闭记录不存在：{closure_id}")
            return {
                "closure_id": closure.closure_id,
                "space_id": closure.space_id,
                "path": self.path_of(closure.space_id),
                "reason": closure.reason,
                "start": closure.start.isoformat(),
                "end": closure.end.isoformat(),
                "leaves": sorted(self.path_of(item) for item in self._leaves(closure.space_id)),
            }

    def space_occupancy(
        self, space_id: str, start: datetime | None = None, end: datetime | None = None
    ) -> dict:
        """空间在指定时段内的占用（含子孙区域上的预约与关闭）。"""
        with self._lock:
            self._require_space(space_id)
            leaves = self._leaves(space_id)

            def in_window(s: datetime, e: datetime) -> bool:
                return start is None or end is None or (start < e and s < end)

            bookings: dict[str, dict] = {}
            closures: dict[str, dict] = {}
            for leaf in leaves:
                for booking_id in self._occupancy.get(leaf, ()):
                    booking = self._bookings[booking_id]
                    if booking_id not in bookings and in_window(booking.start, booking.end):
                        bookings[booking_id] = {
                            "booking_id": booking.booking_id,
                            "activity_name": booking.activity_name,
                            "state": booking.state.value,
                            "path": self.path_of(booking.space_id),
                            "start": booking.start.isoformat(),
                            "end": booking.end.isoformat(),
                        }
                for closure_id in self._closure_leaves.get(leaf, ()):
                    closure = self._closures[closure_id]
                    if closure_id not in closures and in_window(closure.start, closure.end):
                        closures[closure_id] = {
                            "closure_id": closure.closure_id,
                            "reason": closure.reason,
                            "path": self.path_of(closure.space_id),
                            "start": closure.start.isoformat(),
                            "end": closure.end.isoformat(),
                        }
            return {
                "space_id": space_id,
                "path": self.path_of(space_id),
                "leaves": sorted(self.path_of(item) for item in leaves),
                "bookings": sorted(bookings.values(), key=lambda item: item["start"]),
                "closures": sorted(closures.values(), key=lambda item: item["start"]),
            }

    # ------------------------------------------------------------------
    # 层级工具
    # ------------------------------------------------------------------
    def path_of(self, space_id: str) -> str:
        """空间路径，如 市民文化中心/综合展厅/东区，用于冲突路径展示。"""
        space = self._spaces.get(space_id)
        if space is None:
            raise NotFoundError(f"空间不存在：{space_id}")
        names = []
        while space is not None:
            names.append(space.name)
            space = self._spaces.get(space.parent_id)
        return "/".join(reversed(names))

    def _ancestors_of(self, space_id: str) -> list[str]:
        ancestors = []
        current = self._spaces[space_id].parent_id
        while current is not None:
            ancestors.append(current)
            current = self._spaces[current].parent_id
        ancestors.reverse()
        return ancestors

    def _leaves(self, space_id: str) -> frozenset[str]:
        """空间子树的叶子集合：预约展开后的实际物理占用范围。"""
        cached = self._leaf_cache.get(space_id)
        if cached is not None:
            return cached
        children = self._children.get(space_id, [])
        if not children:
            result = frozenset({space_id})
        else:
            collected: set[str] = set()
            for child in children:
                collected |= self._leaves(child)
            result = frozenset(collected)
        self._leaf_cache[space_id] = result
        return result

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _release_occupancy(self, booking: Booking) -> None:
        for leaf in booking.occupied_leaves or ():
            ids = self._occupancy.get(leaf)
            if ids:
                ids.discard(booking.booking_id)

    def _require_space(self, space_id: str) -> Space:
        space = self._spaces.get(space_id)
        if space is None:
            raise NotFoundError(f"空间不存在：{space_id}")
        return space

    def _require_layouts(self, layout_id: str) -> list[Layout]:
        versions = self._layouts.get(layout_id)
        if not versions:
            raise NotFoundError(f"布局不存在：{layout_id}")
        return versions

    def _layout_version(self, layout_id: str, version: int | None) -> Layout:
        versions = self._require_layouts(layout_id)
        for layout in versions:
            if layout.version == version:
                return layout
        raise NotFoundError(f"布局 {layout_id} 没有版本 {version}")

    def _require_booking(self, booking_id: str) -> Booking:
        booking = self._bookings.get(booking_id)
        if booking is None:
            raise NotFoundError(f"预约不存在：{booking_id}")
        return booking

    @staticmethod
    def _require_state(booking: Booking, expected: BookingState, action: str) -> None:
        if booking.state is not expected:
            raise StateError(
                f"只有{expected.value}状态可以{action}，当前状态：{booking.state.value}"
            )

    @staticmethod
    def _check_layout_params(capacity: int, conversion_minutes: int) -> None:
        if capacity <= 0:
            raise ValidationError("布局容量必须为正数")
        if conversion_minutes < 0:
            raise ValidationError("转换缓冲不能为负数")

    @staticmethod
    def _snapshot(layout: Layout) -> dict:
        return {
            "layout_id": layout.layout_id,
            "version": layout.version,
            "name": layout.name,
            "capacity": layout.capacity,
            "equipment": sorted(layout.equipment),
            "conversion_minutes": layout.conversion_minutes,
        }
