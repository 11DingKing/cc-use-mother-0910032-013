"""应用服务：所有写流程在仓储锁内原子完成。

事务模板::

    with store.lock:
        # 1) 读取最新状态
        # 2) 校验需求 / 展开占用快照
        # 3) 冲突检测
        # 4) 写入并推进版本号

检测与写入之间不存在释放锁的间隙，配合事件版本号（CAS），
并发确认中落败方得到 409 与完整冲突路径。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .engine import Engine
from .errors import ConflictError, StateError, ValidationError, VenueError
from .models import (
    CANCELLED,
    CONFIRMED,
    DRAFT,
    Event,
    Layout,
    LayoutZone,
    PENDING,
    RUNNING,
    SETTLED,
    Space,
)
from .repository import VenueStore
from .timeutils import parse_dt


class VenueService:
    def __init__(self, store: VenueStore | None = None) -> None:
        self.store = store or VenueStore()
        self.engine = Engine(self.store)

    # ------------------------------------------------------------------
    # 空间层级
    # ------------------------------------------------------------------
    def create_space(self, data: dict[str, Any]) -> dict[str, Any]:
        name = _require_str(data, "name")
        parent_id = data.get("parent_id")
        buffer_minutes = int(data.get("buffer_minutes", 0))
        if buffer_minutes < 0:
            raise ValidationError("缓冲时间不能为负")
        with self.store.lock:
            space = Space(
                space_id=data.get("space_id") or self.store.next_space_id(),
                name=name,
                parent_id=parent_id,
                buffer_minutes=buffer_minutes,
            )
            self.engine.add_space(space)
            return space.to_dict()

    def list_spaces(self) -> list[dict[str, Any]]:
        with self.store.lock:
            return [s.to_dict() for s in self.store.iter_spaces()]

    def get_space(self, space_id: str) -> dict[str, Any]:
        with self.store.lock:
            return self.store.get_space(space_id).to_dict()

    def space_tree(self) -> list[dict[str, Any]]:
        with self.store.lock:
            nodes = {s.space_id: {**s.to_dict(), "children": []}
                     for s in self.store.iter_spaces()}
            roots: list[dict[str, Any]] = []
            for node in nodes.values():
                parent = node["parent_id"]
                if parent and parent in nodes:
                    nodes[parent]["children"].append(node)
                else:
                    roots.append(node)
            return roots

    # ------------------------------------------------------------------
    # 布局版本
    # ------------------------------------------------------------------
    def create_layout(self, data: dict[str, Any]) -> dict[str, Any]:
        space_id = _require_str(data, "space_id")
        name = _require_str(data, "name")
        zones_in = data.get("zones", [])
        if not isinstance(zones_in, list) or not zones_in:
            raise ValidationError("布局至少要定义一个可排期区域")
        with self.store.lock:
            space = self.store.get_space(space_id)
            version = max((lay.version for lay in space.layouts.values()),
                          default=0) + 1
            layout = Layout(
                layout_id=data.get("layout_id") or self.store.next_layout_id(),
                space_id=space_id,
                version=version,
                name=name,
                created_at=_now(),
            )
            space.layouts[layout.layout_id] = layout
            for item in zones_in:
                zone = LayoutZone(
                    zone_id=item.get("zone_id") or self.store.next_zone_id(),
                    name=_require_str(item, "name", where="zone"),
                    space_id=item.get("space_id", space_id),
                    capacity=int(item.get("capacity", 0)),
                    equipment=set(item.get("equipment", [])),
                )
                if zone.capacity < 0:
                    raise ValidationError("容量不能为负")
                self.engine.add_zone(layout, zone)
            # 首个布局自动生效
            if space.active_layout_id is None:
                layout.active_from = _now()
            return layout.to_dict()

    def activate_layout(self, data: dict[str, Any]) -> dict[str, Any]:
        """布局切换（原子）。

        需要 ``layout_id`` 与转换窗口：``effective_at``（新生效时间）和
        ``setup_minutes``（转换耗时），窗口为 ``[effective_at-setup, effective_at]``。
        窗口内目标空间子树上存在任何占用（含祖先/子区域）即拒绝，
        已完成（已结算）活动保留原布局快照，不参与阻挡。
        """
        layout_id = _require_str(data, "layout_id")
        effective_at = parse_dt(_require(data, "effective_at"))
        setup_minutes = int(data.get("setup_minutes", 0))
        if setup_minutes < 0:
            raise ValidationError("转换耗时不能为负")
        window_start = effective_at - timedelta(minutes=setup_minutes)

        with self.store.lock:
            new_layout = self.store.get_layout(layout_id)
            space = self.store.get_space(new_layout.space_id)
            current_id = space.active_layout_id
            if current_id == layout_id:
                raise ValidationError(f"布局 {layout_id} 已是生效布局")
            conflicts = self.engine.detect_switch(
                space, window_start, effective_at)
            if conflicts:
                raise ConflictError(
                    f"空间 {space.name} 在转换窗口内仍有占用，无法切换布局",
                    {
                        "conflicts": conflicts,
                        "conversion_window": {
                            "start": window_start.isoformat(),
                            "end": effective_at.isoformat(),
                        },
                    },
                )
            for lay in space.layouts.values():
                lay.active_from = None
            new_layout.active_from = effective_at
            return {
                "activated": new_layout.to_dict(),
                "previous_layout_id": current_id,
                "conversion_window": {
                    "start": window_start.isoformat(),
                    "end": effective_at.isoformat(),
                },
            }

    # ------------------------------------------------------------------
    # 部分关闭
    # ------------------------------------------------------------------
    def add_closure(self, data: dict[str, Any]) -> dict[str, Any]:
        space_id = _require_str(data, "space_id")
        start = parse_dt(_require(data, "start"))
        end = parse_dt(_require(data, "end"))
        if end <= start:
            raise ValidationError("关闭结束时间必须晚于开始时间")
        child_ids = list(data.get("space_ids", []))
        with self.store.lock:
            space = self.store.get_space(space_id)
            blocked: set[str] = set()
            if child_ids:
                for cid in child_ids:
                    child = self.store.get_space(cid)
                    if child.space_id not in self.engine.descendants(space_id):
                        raise ValidationError(
                            f"关闭子区域 {cid} 不在空间 {space_id} 的子树内"
                        )
                    blocked |= self.engine.descendants(cid)
            else:
                blocked = self.engine.descendants(space_id)
            conflicts = self.engine.detect_closure(blocked, start, end)
            if conflicts:
                raise ConflictError(
                    f"空间 {space.name} 关闭时段内仍有占用",
                    {"conflicts": conflicts},
                )
            closure = {
                "start": start,
                "end": end,
                "space_ids": child_ids,
                "reason": data.get("reason"),
            }
            space.closures.append(closure)
            return {
                "space_id": space_id,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "space_ids": child_ids,
                "blocked_space_ids": sorted(blocked),
                "reason": closure["reason"],
            }

    # ------------------------------------------------------------------
    # 活动
    # ------------------------------------------------------------------
    def create_event(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.store.lock:
            event = self._build_event(data)
            self.store.add_event(event)
            return event.to_dict()

    def _build_event(self, data: dict[str, Any], event: Event | None = None) -> Event:
        event = event or Event(
            event_id=data.get("event_id") or self.store.next_event_id(),
            name="",
            created_at=_now(),
        )
        if "name" in data:
            event.name = _require_str(data, "name")
        elif not event.name:
            raise ValidationError("活动缺少名称")
        event.requested_space_id = data.get(
            "requested_space_id", event.requested_space_id)
        event.requested_zone_id = data.get(
            "requested_zone_id", event.requested_zone_id)
        if "start" in data:
            event.start = parse_dt(data["start"])
        if "end" in data:
            event.end = parse_dt(data["end"])
        if event.start and event.end and event.end <= event.start:
            raise ValidationError("结束时间必须晚于开始时间（跨日活动请直接给出跨日时间）")
        event.attendees = int(data.get("attendees", event.attendees))
        if event.attendees < 0:
            raise ValidationError("人数不能为负")
        event.required_equipment = set(
            data.get("required_equipment", event.required_equipment))
        if "buffer_pre_minutes" in data:
            event.buffer_pre_minutes = data["buffer_pre_minutes"]
        if "buffer_post_minutes" in data:
            event.buffer_post_minutes = data["buffer_post_minutes"]
        for key in ("buffer_pre_minutes", "buffer_post_minutes"):
            value = getattr(event, key)
            if value is not None and int(value) < 0:
                raise ValidationError("缓冲时间不能为负")
        event.updated_at = _now()
        return event

    def update_event(self, event_id: str, data: dict[str, Any]) -> dict[str, Any]:
        """仅筹备（草稿）状态可整体修改需求。"""
        with self.store.lock:
            event = self.store.get_event(event_id)
            if event.state != DRAFT:
                raise StateError(
                    f"活动当前为「{event.state}」，仅「{DRAFT}」状态可修改需求"
                )
            self._build_event(data, event)
            event.version += 1
            return event.to_dict()

    def _bind_snapshot(self, event: Event) -> None:
        snap = self.engine.build_snapshot(event)
        event.snapshot = snap
        event.layout_id = snap.layout_id

    def submit_event(self, event_id: str,
                     data: dict[str, Any] | None = None) -> dict[str, Any]:
        """筹备 → 待确认：校验需求并展开占用快照（软占用）。

        待确认相当于排队候补：不与既有占用互斥，冲突（含其他待确认先落定者）
        在 :meth:`confirm_event` 原子判定，落败方得到冲突路径与替代空间。
        """
        with self.store.lock:
            event = self.store.get_event(event_id)
            if data:
                if event.state != DRAFT:
                    raise StateError(f"活动当前为「{event.state}」，不能随提交修改需求")
                self._build_event(data, event)
            if event.state != DRAFT:
                raise StateError(f"仅「{DRAFT}」活动可提交确认，当前为「{event.state}」")
            self._bind_snapshot(event)
            event.state = PENDING
            event.version += 1
            return event.to_dict()

    def confirm_event(self, event_id: str,
                      expected_version: int | None = None) -> dict[str, Any]:
        """待确认 → 已排定（原子 CAS）。

        持锁重新按当前生效布局展开并检测硬占用/关闭：
        并发确认时先到者成功，后到者得到 409、冲突路径与替代空间。
        若等待期间布局已切换，则拒绝并提示重新提交。
        """
        with self.store.lock:
            event = self.store.get_event(event_id)
            if expected_version is not None and event.version != expected_version:
                raise ConflictError(
                    f"活动版本已变化（期望 {expected_version}，当前 {event.version}）",
                    {"current_version": event.version},
                )
            if event.state == CONFIRMED:
                raise StateError("活动已确认，请勿重复确认")
            if event.state != PENDING:
                raise StateError(
                    f"仅「{PENDING}」活动可确认，当前为「{event.state}」"
                )
            # 等待确认期间布局可能已切换：以当前生效布局重新展开
            try:
                self._bind_snapshot(event)
            except VenueError as exc:
                raise StateError(
                    "排期区域所属布局已切换或调整，请重新提交活动",
                ) from exc
            conflicts = self.engine.detect(
                event.snapshot, exclude_event_id=event_id, hard_only=True)
            if conflicts:
                raise ConflictError(
                    "确认失败：请求时段已被其他活动占用",
                    {
                        "conflicts": conflicts,
                        "alternatives": self.engine.find_alternatives(
                            event, preferred_space_id=event.requested_space_id),
                        "current_version": event.version,
                    },
                )
            event.state = CONFIRMED
            event.version += 1
            event.updated_at = _now()
            return event.to_dict()

    def transition_event(self, event_id: str, action: str) -> dict[str, Any]:
        """开始执行 / 完成结算。完成后保留原布局快照，不再参与冲突检测。"""
        with self.store.lock:
            event = self.store.get_event(event_id)
            if action == "start":
                if event.state != CONFIRMED:
                    raise StateError(f"仅「{CONFIRMED}」活动可开始，当前为「{event.state}」")
                event.state = RUNNING
            elif action == "settle":
                if event.state != RUNNING:
                    raise StateError(f"仅「{RUNNING}」活动可结算，当前为「{event.state}」")
                event.state = SETTLED
            else:
                raise ValidationError(f"未知操作：{action}")
            event.version += 1
            event.updated_at = _now()
            return event.to_dict()

    def cancel_event(self, event_id: str) -> dict[str, Any]:
        with self.store.lock:
            event = self.store.get_event(event_id)
            if event.state in (SETTLED, CANCELLED):
                raise StateError(f"活动已为「{event.state}」，不能取消")
            event.state = CANCELLED
            event.version += 1
            event.updated_at = _now()
            return event.to_dict()

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self.store.lock:
            return self.store.get_event(event_id).to_dict()

    def list_events(self) -> list[dict[str, Any]]:
        with self.store.lock:
            return [e.to_dict() for e in self.store.iter_events()]

    # ------------------------------------------------------------------
    # 可用性查询（不落库）
    # ------------------------------------------------------------------
    def check_availability(self, data: dict[str, Any]) -> dict[str, Any]:
        """按给定需求做一次试排期，返回占用展开、冲突路径与替代空间。"""
        with self.store.lock:
            probe = self._build_event(dict(data), Event(
                event_id="__probe__", name=data.get("name", "试排期"),
                created_at=_now()))
            snap = None
            unmet: list[str] = []
            build_error: str | None = None
            try:
                snap = self.engine.build_snapshot(probe)
            except ConflictError:
                raise
            except Exception as exc:  # 试排期不落库：把校验失败转为解释信息
                build_error = str(exc)
                if probe.requested_zone_id:
                    try:
                        _, zone = self.engine.find_active_zone(
                            probe.requested_zone_id)
                        unmet = self.engine.check_requirements(
                            probe, zone, collect=True)
                    except Exception:
                        pass
            conflicts = self.engine.detect(snap) if snap else []
            return {
                "available": not conflicts and snap is not None,
                "occupancy": snap.to_dict() if snap else None,
                "conflicts": conflicts,
                "unmet_requirements": unmet,
                "message": build_error,
                "alternatives": self.engine.find_alternatives(
                    probe, preferred_space_id=probe.requested_space_id),
            }


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data or data[key] in (None, ""):
        raise ValidationError(f"缺少必填字段：{key}")
    return data[key]


def _require_str(data: dict[str, Any], key: str, *, where: str = "") -> str:
    value = _require(data, key)
    if not isinstance(value, str):
        raise ValidationError(f"字段 {key} 必须是字符串")
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)
