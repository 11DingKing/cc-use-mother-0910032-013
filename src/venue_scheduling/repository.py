"""线程安全的内存仓储。

单进程内使用一把可重入锁串行化所有写事务；事件带版本号实现乐观锁，
HTTP 请求可携带期望版本做 CAS，并发确认时落败方得到明确的 409。
换用真实数据库时，只需把本类替换为同等接口的实现。
"""
from __future__ import annotations

import threading
from typing import Iterable

from .errors import NotFoundError
from .models import HARD_OCCUPYING_STATES, OCCUPYING_STATES, Event, Layout, Space


class VenueStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.spaces: dict[str, Space] = {}
        self.events: dict[str, Event] = {}
        self._space_seq = 0
        self._layout_seq = 0
        self._zone_seq = 0
        self._event_seq = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- id 生成 -------------------------------------------------------
    def next_space_id(self) -> str:
        self._space_seq += 1
        return f"S{self._space_seq:03d}"

    def next_layout_id(self) -> str:
        self._layout_seq += 1
        return f"L{self._layout_seq:03d}"

    def next_zone_id(self) -> str:
        self._zone_seq += 1
        return f"Z{self._zone_seq:03d}"

    def next_event_id(self) -> str:
        self._event_seq += 1
        return f"E{self._event_seq:04d}"

    # ---- space ---------------------------------------------------------
    def add_space(self, space: Space) -> None:
        if space.space_id in self.spaces:
            raise ValueError(f"空间编号已存在：{space.space_id}")
        self.spaces[space.space_id] = space

    def get_space(self, space_id: str) -> Space:
        space = self.spaces.get(space_id)
        if space is None:
            raise NotFoundError(f"空间不存在：{space_id}")
        return space

    def find_space(self, space_id: str) -> Space | None:
        return self.spaces.get(space_id)

    def iter_spaces(self) -> Iterable[Space]:
        return list(self.spaces.values())

    # ---- layout --------------------------------------------------------
    def add_layout(self, layout: Layout) -> None:
        space = self.get_layout_owner(layout.layout_id)
        if space is not None:
            raise ValueError(f"布局编号已存在：{layout.layout_id}")
        self.get_space(layout.space_id).layouts[layout.layout_id] = layout

    def get_layout_owner(self, layout_id: str) -> Space | None:
        for space in self.spaces.values():
            if layout_id in space.layouts:
                return space
        return None

    def get_layout(self, layout_id: str) -> Layout:
        space = self.get_layout_owner(layout_id)
        if space is None:
            raise NotFoundError(f"布局不存在：{layout_id}")
        return space.layouts[layout_id]

    # ---- event ---------------------------------------------------------
    def add_event(self, event: Event) -> None:
        if event.event_id in self.events:
            raise ValueError(f"活动编号已存在：{event.event_id}")
        self.events[event.event_id] = event

    def get_event(self, event_id: str) -> Event:
        event = self.events.get(event_id)
        if event is None:
            raise NotFoundError(f"活动不存在：{event_id}")
        return event

    def iter_events(self) -> Iterable[Event]:
        return list(self.events.values())

    def occupying_events(self, exclude: str | None = None,
                         hard_only: bool = False) -> list[Event]:
        states = HARD_OCCUPYING_STATES if hard_only else OCCUPYING_STATES
        return [
            e for e in self.events.values()
            if e.state in states and e.event_id != exclude
        ]
