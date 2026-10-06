"""核心领域引擎：空间树、占用快照、层级冲突检测与替代空间推荐。

无锁纯逻辑；所有公开写流程由 :class:`~venue_scheduling.service.VenueService`
在仓储锁内调用，保证检测与写入的原子性。

冲突判定模型
------------
- 时间：活动占用区间 = 请求区间前后各展开转换/清场缓冲，左闭右开。
- 空间：每个可排期区域（zone）落在物理空间树的一个节点上；
  两次占用的空间链（节点 + 全部祖先）相交即为空间冲突，
  因此"订了整个展厅"与"订了隔断分区"互为祖先/子区域冲突，
  且跨布局版本同样成立（都按物理树判定）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    Event,
    Layout,
    LayoutZone,
    OccupancySnapshot,
    Space,
)
from .repository import VenueStore
from .timeutils import expand, intersection, overlap

# 冲突关系标签
SAME = "同区域占用"
ANCESTOR = "祖先空间已被占用"
DESCENDANT = "子区域已被占用"


class Engine:
    def __init__(self, store: VenueStore) -> None:
        self.store = store

    # ---- 空间树 --------------------------------------------------------
    def ancestors(self, space_id: str, include_self: bool = True) -> list[Space]:
        """从根到该节点的链。"""
        chain: list[Space] = []
        current: Space | None = self.store.get_space(space_id)
        while current is not None:
            chain.append(current)
            current = self.store.find_space(current.parent_id) if current.parent_id else None
        chain.reverse()
        if not include_self:
            chain = [s for s in chain if s.space_id != space_id]
        return chain

    def ancestor_ids(self, space_id: str) -> list[str]:
        return [s.space_id for s in self.ancestors(space_id)]

    def descendants(self, space_id: str, include_self: bool = True) -> set[str]:
        result = {space_id} if include_self else set()
        changed = True
        while changed:
            changed = False
            for space in self.store.iter_spaces():
                if space.parent_id in result and space.space_id not in result:
                    result.add(space.space_id)
                    changed = True
        return result

    def path_names(self, space_id: str) -> list[dict[str, str]]:
        return [{"space_id": s.space_id, "name": s.name}
                for s in self.ancestors(space_id)]

    def _check_tree_integrity(self, space: Space) -> None:
        if space.parent_id is not None:
            parent = self.store.find_space(space.parent_id)
            if parent is None:
                raise ValidationError(f"父空间不存在：{space.parent_id}")
        # 防环
        seen: set[str] = set()
        cur: Space | None = space
        while cur is not None:
            if cur.space_id in seen:
                raise ValidationError("空间层级出现环路")
            seen.add(cur.space_id)
            cur = self.store.find_space(cur.parent_id) if cur.parent_id else None

    def add_space(self, space: Space) -> Space:
        if space.parent_id == space.space_id:
            raise ValidationError("空间的父节点不能是自身")
        self._check_tree_integrity(space)
        self.store.add_space(space)
        return space

    def add_zone(self, layout: Layout, zone: LayoutZone) -> None:
        target = self.store.get_space(zone.space_id)
        owner = self.store.get_space(layout.space_id)
        if target.space_id not in self.descendants(owner.space_id):
            raise ValidationError(
                f"区域 {zone.zone_id} 必须位于布局所属空间 {owner.space_id} 的子树内"
            )
        if zone.zone_id in layout.zones:
            raise ValidationError(f"布局内区域编号重复：{zone.zone_id}")
        layout.zones[zone.zone_id] = zone

    # ---- 布局/区域解析 --------------------------------------------------
    def find_zone(self, zone_id: str) -> tuple[Layout, LayoutZone]:
        for space in self.store.iter_spaces():
            for layout in space.layouts.values():
                zone = layout.zones.get(zone_id)
                if zone is not None:
                    return layout, zone
        raise NotFoundError(f"区域不存在：{zone_id}")

    def find_active_zone(self, zone_id: str) -> tuple[Layout, LayoutZone]:
        layout, zone = self.find_zone(zone_id)
        if not layout.is_active:
            raise ValidationError(
                f"区域 {zone_id} 所属布局 {layout.layout_id}（{layout.name}）未生效"
            )
        return layout, zone

    # ---- 需求与快照 ----------------------------------------------------
    def _buffers_for(self, event: Event, space: Space) -> tuple[int, int]:
        pre = event.buffer_pre_minutes
        post = event.buffer_post_minutes
        if pre is None:
            pre = space.buffer_minutes
        if post is None:
            post = space.buffer_minutes
        if pre < 0 or post < 0:
            raise ValidationError("缓冲时间不能为负")
        return pre, post

    def check_requirements(self, event: Event, zone: LayoutZone,
                           *, collect: bool = False) -> list[str]:
        """返回未满足的需求说明列表；空列表表示满足。"""
        problems: list[str] = []
        if event.attendees > zone.capacity:
            problems.append(
                f"容量不足：需要 {event.attendees} 人，区域 {zone.name} 仅 {zone.capacity} 人"
            )
        missing = event.required_equipment - zone.equipment
        if missing:
            problems.append(
                "缺少设备：" + "、".join(sorted(missing))
                + f"（区域 {zone.name} 现有："
                + ("、".join(sorted(zone.equipment)) or "无") + "）"
            )
        if not collect and problems:
            raise ValidationError("；".join(problems), {"unmet": problems})
        return problems

    def build_snapshot(self, event: Event) -> OccupancySnapshot:
        """按当前生效布局把活动需求展开为实际占用范围（不做冲突检测）。"""
        if not event.start or not event.end:
            raise ValidationError("活动缺少起止时间")
        if event.end <= event.start:
            raise ValidationError("结束时间必须晚于开始时间")
        if event.attendees < 0:
            raise ValidationError("人数不能为负")
        if not event.requested_zone_id:
            raise ValidationError("活动未指定排期区域")

        layout, zone = self.find_active_zone(event.requested_zone_id)
        # 请求空间可以是布局属主，也可以是区域实际落点（或其任一祖先）
        if event.requested_space_id:
            allowed = self.descendants(event.requested_space_id)
            if zone.space_id not in allowed:
                raise ValidationError(
                    f"区域 {zone.zone_id} 不在所请求空间 "
                    f"{event.requested_space_id} 的层级内"
                )
        self.check_requirements(event, zone)

        space = self.store.get_space(zone.space_id)
        pre, post = self._buffers_for(event, space)
        occ_start, occ_end = expand(event.start, event.end, pre, post)
        return OccupancySnapshot(
            space_id=space.space_id,
            space_name=space.name,
            layout_id=layout.layout_id,
            layout_name=layout.name,
            zone_id=zone.zone_id,
            zone_name=zone.name,
            occ_start=occ_start,
            occ_end=occ_end,
            req_start=event.start,
            req_end=event.end,
            capacity=zone.capacity,
            equipment=sorted(zone.equipment),
            space_chain=self.ancestor_ids(space.space_id),
        )

    # ---- 冲突检测 ------------------------------------------------------
    def _event_conflict(self, snap: OccupancySnapshot,
                        other: Event) -> dict[str, Any] | None:
        other_snap = other.snapshot
        assert other_snap is not None
        if not overlap(snap.occ_start, snap.occ_end,
                       other_snap.occ_start, other_snap.occ_end):
            return None
        # 层级冲突仅当一个占用节点是另一个的祖先（或二者相同）；
        # 仅共享祖先（兄弟隔断分区）不冲突。
        if snap.space_id == other_snap.space_id:
            relation = SAME
            conflict_space_id = snap.space_id
        elif other_snap.space_id in snap.space_chain:
            # 既有活动占用祖先节点（已订整厅，再订隔断分区）
            relation = ANCESTOR
            conflict_space_id = other_snap.space_id
        elif snap.space_id in other_snap.space_chain:
            # 既有活动占用后代节点（已订隔断分区，再订整厅）
            relation = DESCENDANT
            conflict_space_id = snap.space_id
        else:
            return None

        win_start, win_end = intersection(
            snap.occ_start, snap.occ_end,
            other_snap.occ_start, other_snap.occ_end,
        )
        return {
            "type": "event",
            "relationship": relation,
            "conflict_space_id": conflict_space_id,
            "request_path": self.path_names(snap.space_id),
            "occupied_path": self.path_names(other_snap.space_id),
            "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
            "event_id": other.event_id,
            "event_name": other.name,
            "event_state": other.state,
            "layout_id": other_snap.layout_id,
            "layout_name": other_snap.layout_name,
            "zone_id": other_snap.zone_id,
            "zone_name": other_snap.zone_name,
            "occupied_interval": {
                "start": other_snap.occ_start.isoformat(),
                "end": other_snap.occ_end.isoformat(),
            },
        }

    def _closure_conflicts(self, snap: OccupancySnapshot) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        snap_space = self.store.get_space(snap.space_id)
        for space in self.store.iter_spaces():
            for closure in space.closures:
                if not overlap(snap.occ_start, snap.occ_end,
                               closure["start"], closure["end"]):
                    continue
                blocked: set[str]
                if closure["space_ids"]:
                    blocked = set()
                    for sid in closure["space_ids"]:
                        blocked |= self.descendants(sid)
                else:
                    blocked = self.descendants(space.space_id)
                if snap.space_id not in blocked:
                    continue
                win_start, win_end = intersection(
                    snap.occ_start, snap.occ_end,
                    closure["start"], closure["end"],
                )
                conflicts.append({
                    "type": "closure",
                    "relationship": "空间关闭",
                    "conflict_space_id": snap.space_id,
                    "request_path": self.path_names(snap.space_id),
                    "occupied_path": self.path_names(space.space_id),
                    "window": {"start": win_start.isoformat(),
                               "end": win_end.isoformat()},
                    "closure": {
                        "space_id": space.space_id,
                        "space_name": space.name,
                        "space_ids": list(closure["space_ids"]),
                        "reason": closure.get("reason"),
                    },
                })
        return conflicts

    def detect(self, snap: OccupancySnapshot,
               exclude_event_id: str | None = None,
               *, hard_only: bool = False) -> list[dict[str, Any]]:
        """对一份待占用快照检测全部冲突（活动占用 + 关闭）。

        ``hard_only=True`` 时只比对硬占用（已排定/执行中），
        用于提交待确认；确认落定与试排期使用全量比对。
        """
        conflicts: list[dict[str, Any]] = []
        for other in self.store.occupying_events(
                exclude=exclude_event_id, hard_only=hard_only):
            entry = self._event_conflict(snap, other)
            if entry:
                conflicts.append(entry)
        conflicts.extend(self._closure_conflicts(snap))
        return conflicts

    def ensure_available(self, snap: OccupancySnapshot,
                         exclude_event_id: str | None = None) -> list[dict[str, Any]]:
        conflicts = self.detect(snap, exclude_event_id=exclude_event_id)
        if conflicts:
            raise ConflictError(
                f"空间 {snap.space_name}/{snap.zone_name} 在请求时段存在冲突",
                {"conflicts": conflicts},
            )
        return conflicts

    # ---- 布局切换 / 关闭的冲突检测 --------------------------------------
    def detect_switch(self, space: Space, window_start: datetime,
                      window_end: datetime) -> list[dict[str, Any]]:
        """转换窗口内，目标空间子树上的既有占用都会阻碍切换。

        零耗时切换（``window_start == window_end``）按点时刻判定：
        该时刻仍在进行的占用（``occ_start <= t < occ_end``）同样阻挡。
        """
        instant = window_start == window_end
        subtree = self.descendants(space.space_id)
        conflicts: list[dict[str, Any]] = []
        for other in self.store.occupying_events():
            snap = other.snapshot
            assert snap is not None
            if snap.space_id not in subtree:
                continue
            if instant:
                if not (snap.occ_start <= window_start < snap.occ_end):
                    continue
                win_start, win_end = window_start, window_end
            else:
                if not overlap(window_start, window_end,
                               snap.occ_start, snap.occ_end):
                    continue
                win_start, win_end = intersection(
                    window_start, window_end, snap.occ_start, snap.occ_end)
            conflicts.append({
                "type": "event",
                "relationship": "转换缓冲期内仍有占用",
                "conflict_space_id": snap.space_id,
                "request_path": self.path_names(space.space_id),
                "occupied_path": self.path_names(snap.space_id),
                "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
                "event_id": other.event_id,
                "event_name": other.name,
                "event_state": other.state,
                "layout_id": snap.layout_id,
                "layout_name": snap.layout_name,
                "zone_name": snap.zone_name,
            })
        return conflicts

    def detect_closure(self, blocked: set[str], start: datetime,
                       end: datetime) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for other in self.store.occupying_events():
            snap = other.snapshot
            assert snap is not None
            if snap.space_id not in blocked:
                continue
            if not overlap(start, end, snap.occ_start, snap.occ_end):
                continue
            win_start, win_end = intersection(
                start, end, snap.occ_start, snap.occ_end)
            conflicts.append({
                "type": "event",
                "relationship": "关闭时段内仍有占用",
                "conflict_space_id": snap.space_id,
                "request_path": self.path_names(snap.space_id),
                "occupied_path": self.path_names(snap.space_id),
                "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
                "event_id": other.event_id,
                "event_name": other.name,
                "event_state": other.state,
                "layout_name": snap.layout_name,
                "zone_name": snap.zone_name,
            })
        return conflicts

    # ---- 替代空间 ------------------------------------------------------
    def iter_active_zones(self) -> Iterable[tuple[Space, Layout, LayoutZone]]:
        for space in self.store.iter_spaces():
            layout = space.active_layout()
            if layout is None:
                continue
            for zone in layout.zones.values():
                yield space, layout, zone

    def find_alternatives(self, event: Event, *,
                          limit: int = 10,
                          preferred_space_id: str | None = None
                          ) -> list[dict[str, Any]]:
        """枚举满足容量/设备且在请求时段（含缓冲）无冲突的可行替代区域。

        排序：与偏好空间同父级的区域优先，其次按容量贴合度（富余最少）。
        """
        if not event.start or not event.end:
            return []
        candidates: list[tuple[int, int, dict[str, Any]]] = []
        preferred_parent = (
            self.store.get_space(preferred_space_id).parent_id
            if preferred_space_id and self.store.find_space(preferred_space_id)
            else None
        )

        for owner_space, layout, zone in self.iter_active_zones():
            if zone.zone_id == event.requested_zone_id:
                continue
            problems = self.check_requirements(event, zone, collect=True)
            if problems:
                continue
            zone_space = self.store.get_space(zone.space_id)
            pre, post = self._buffers_for(event, zone_space)
            occ_start, occ_end = expand(event.start, event.end, pre, post)
            probe = OccupancySnapshot(
                space_id=zone_space.space_id,
                space_name=zone_space.name,
                layout_id=layout.layout_id,
                layout_name=layout.name,
                zone_id=zone.zone_id,
                zone_name=zone.name,
                occ_start=occ_start,
                occ_end=occ_end,
                req_start=event.start,
                req_end=event.end,
                capacity=zone.capacity,
                equipment=sorted(zone.equipment),
                space_chain=self.ancestor_ids(zone_space.space_id),
            )
            if self.detect(probe):
                continue
            same_parent = 0 if (preferred_parent and
                                zone_space.parent_id == preferred_parent) else 1
            candidates.append((
                same_parent,
                zone.capacity - event.attendees,
                {
                    "space_id": zone_space.space_id,
                    "space_name": zone_space.name,
                    "path": [node["name"] for node in self.path_names(zone_space.space_id)],
                    "layout_id": layout.layout_id,
                    "layout_name": layout.name,
                    "zone_id": zone.zone_id,
                    "zone_name": zone.name,
                    "capacity": zone.capacity,
                    "equipment": sorted(zone.equipment),
                    "buffer_pre_minutes": pre,
                    "buffer_post_minutes": post,
                },
            ))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in candidates[:limit]]
