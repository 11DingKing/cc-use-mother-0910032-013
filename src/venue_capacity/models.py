"""领域模型：空间层级、布局版本、活动需求、预约与部分关闭。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class BookingState(str, Enum):
    """活动状态，与领域契约 states 对齐。"""

    DRAFT = "筹备"
    PENDING = "待确认"
    SCHEDULED = "已排定"
    IN_PROGRESS = "执行中"
    SETTLED = "已结算"
    CANCELLED = "已取消"


# 已排定及之后都视为占用；已结算保留历史占用，便于追溯且不可复用过去时段
OCCUPYING_STATES = frozenset(
    {BookingState.SCHEDULED, BookingState.IN_PROGRESS, BookingState.SETTLED}
)


@dataclass(frozen=True)
class Space:
    """空间节点；可拆分展厅通过子空间表达（如综合展厅 → 东区/西区）。"""

    space_id: str
    name: str
    parent_id: str | None = None


@dataclass(frozen=True)
class Layout:
    """布局的不可变版本；修改布局会产生新版本，旧版本保留给历史活动。"""

    layout_id: str
    space_id: str
    version: int
    name: str
    capacity: int
    equipment: frozenset[str]
    # 从其它布局转换到本布局所需的缓冲分钟数
    conversion_minutes: int = 0
    retired: bool = False


@dataclass(frozen=True)
class ActivityRequirement:
    """活动需求：人数与设备。"""

    capacity: int
    equipment: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Closure:
    """部分关闭：某空间在一段时间内不可用（检修、封场等）。"""

    closure_id: str
    space_id: str
    start: datetime
    end: datetime
    reason: str = ""


@dataclass
class Booking:
    """活动预约。布局版本在提交时钉住，确认时展开占用并留存布局快照。"""

    booking_id: str
    activity_name: str
    space_id: str
    layout_id: str
    start: datetime
    end: datetime  # 结束时间，开区间；支持跨日
    requirement: ActivityRequirement
    state: BookingState = BookingState.DRAFT
    layout_version: int | None = None  # 提交时钉住的布局版本
    layout_snapshot: dict[str, Any] | None = None  # 确认时的布局快照，已结算后仍保留
    occupied_leaves: frozenset[str] | None = None  # 确认时展开的实际占用叶子空间
