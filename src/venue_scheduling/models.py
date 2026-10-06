"""领域模型：空间、布局、活动与关闭记录。

层级关系挂在空间上（稳定的物理树），容量/设备/拆分形态挂在布局版本上
（同一物理空间在不同布局下不同）。活动一旦确认即固化布局快照，
使后续布局切换不影响已排定（含已完成）活动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutils import fmt

# 活动生命周期
DRAFT = "筹备"        # 草稿：不产生占用
PENDING = "待确认"    # 待确认：产生占用（软占用），可被并发确认挤出
CONFIRMED = "已排定"  # 已排定：产生占用，硬占用
RUNNING = "执行中"    # 执行中：产生占用
SETTLED = "已结算"    # 已结算（已完成）：保留占用快照与原布局，不再阻挡新布局切换
CANCELLED = "已取消"

OCCUPYING_STATES = frozenset({PENDING, CONFIRMED, RUNNING})
# 硬占用：已排定、执行中。待确认为软占用，不阻塞其他待确认，
# 但确认时必须与全部硬占用及关闭记录重新比对。
HARD_OCCUPYING_STATES = frozenset({CONFIRMED, RUNNING})
FINISHED_STATES = frozenset({SETTLED})

# 状态推进顺序（终态除外）
STATE_ORDER = [DRAFT, PENDING, CONFIRMED, RUNNING, SETTLED]


@dataclass
class LayoutZone:
    """某布局版本下一个可独立排期的区域。

    布局 A（全开）中整个展厅只有一个 zone，``space_id`` 指向展厅节点；
    布局 B（拆分）中多个 zone 的 ``space_id`` 分别指向展厅的隔断子空间。
    容量与设备条件按布局版本区分：同一物理区域在不同布局下数值不同。
    冲突检测以物理空间树的包含关系为准（祖先与后代互斥），
    与活动使用的是哪一个布局版本无关。
    """

    zone_id: str
    name: str
    space_id: str
    capacity: int
    equipment: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "name": self.name,
            "space_id": self.space_id,
            "capacity": self.capacity,
            "equipment": sorted(self.equipment),
        }


@dataclass
class Layout:
    """空间的一个布局版本。

    - ``active``：当前生效版本，每个空间至多一个生效版本。
    - ``zones``：该版本下的实际可排期区域。
    - 切换生效需要缓冲期：从其他布局切走的活动结束后要做隔断转换。
    """

    layout_id: str
    space_id: str
    version: int
    name: str
    zones: dict[str, LayoutZone] = field(default_factory=dict)
    active_from: datetime | None = None
    created_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.active_from is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "layout_id": self.layout_id,
            "space_id": self.space_id,
            "version": self.version,
            "name": self.name,
            "active": self.is_active,
            "active_from": fmt(self.active_from) if self.active_from else None,
            "created_at": fmt(self.created_at) if self.created_at else None,
            "zones": [z.to_dict() for z in self.zones.values()],
        }


@dataclass
class Space:
    """空间层级节点（场馆/楼层/展厅/隔断分区……）。"""

    space_id: str
    name: str
    parent_id: str | None = None
    # 切换进/出该空间的隔断转换需要的缓冲分钟数（默认值，活动可覆盖）
    buffer_minutes: int = 0
    layouts: dict[str, Layout] = field(default_factory=dict)
    # 部分关闭：[{start,end,space_ids,reason}]；space_ids 为受影响的物理
    # 子空间，空列表表示整个空间关闭
    closures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, with_layouts: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "space_id": self.space_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "buffer_minutes": self.buffer_minutes,
        }
        if with_layouts:
            data["layouts"] = [lay.to_dict() for lay in self.layouts.values()]
            data["active_layout_id"] = self.active_layout_id
            data["closures"] = [
                {
                    "start": fmt(c["start"]),
                    "end": fmt(c["end"]),
                    "space_ids": list(c["space_ids"]),
                    "reason": c.get("reason"),
                }
                for c in self.closures
            ]
        return data

    @property
    def active_layout_id(self) -> str | None:
        for lay in self.layouts.values():
            if lay.is_active:
                return lay.layout_id
        return None

    def active_layout(self) -> Layout | None:
        for lay in self.layouts.values():
            if lay.is_active:
                return lay
        return None

    def get_layout(self, layout_id: str) -> Layout:
        return self.layouts[layout_id]


@dataclass
class OccupancySnapshot:
    """活动确认时固化的实际占用范围（展开后），作为冲突检测的唯一依据。"""

    space_id: str          # zone 对应的物理空间节点（实际占用落点）
    space_name: str
    layout_id: str
    layout_name: str
    zone_id: str
    zone_name: str
    # 展开缓冲后的实际占用区间
    occ_start: datetime
    occ_end: datetime
    # 用户请求的原始区间
    req_start: datetime
    req_end: datetime
    capacity: int
    equipment: list[str]
    # 占用涉及的空间链（自身 + 全部祖先），祖先/子区域冲突检测用
    space_chain: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "space_id": self.space_id,
            "space_name": self.space_name,
            "layout_id": self.layout_id,
            "layout_name": self.layout_name,
            "zone_id": self.zone_id,
            "zone_name": self.zone_name,
            "occ_start": fmt(self.occ_start),
            "occ_end": fmt(self.occ_end),
            "req_start": fmt(self.req_start),
            "req_end": fmt(self.req_end),
            "capacity": self.capacity,
            "equipment": sorted(self.equipment),
            "space_chain": list(self.space_chain),
        }


@dataclass
class Event:
    """活动及其需求。

    状态为"待确认/已排定/执行中"时按 ``snapshot`` 产生占用；
    "已结算"保留快照与原布局（仅作记录，不再阻挡新布局）；
    "已取消"不产生占用。
    """

    event_id: str
    name: str
    state: str = DRAFT
    requested_space_id: str | None = None
    requested_zone_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    attendees: int = 0
    required_equipment: set[str] = field(default_factory=set)
    buffer_pre_minutes: int | None = None
    buffer_post_minutes: int | None = None
    layout_id: str | None = None      # 确认时绑定，之后不变
    snapshot: OccupancySnapshot | None = None
    version: int = 0                  # 乐观锁版本
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def occupies(self) -> bool:
        return self.state in OCCUPYING_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "state": self.state,
            "requested_space_id": self.requested_space_id,
            "requested_zone_id": self.requested_zone_id,
            "start": fmt(self.start) if self.start else None,
            "end": fmt(self.end) if self.end else None,
            "attendees": self.attendees,
            "required_equipment": sorted(self.required_equipment),
            "buffer_pre_minutes": self.buffer_pre_minutes,
            "buffer_post_minutes": self.buffer_post_minutes,
            "layout_id": self.layout_id,
            "version": self.version,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }
