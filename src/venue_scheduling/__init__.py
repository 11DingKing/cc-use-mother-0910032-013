"""场地容量冲突治理后端。

维护可拆分展厅的空间层级、布局版本、容量/设备条件与转换缓冲，
提供层级冲突检测、原子并发确认、布局快照与替代空间推荐。
"""
from .errors import (
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
    VenueError,
)
from .repository import VenueStore
from .service import VenueService

__all__ = [
    "VenueService",
    "VenueStore",
    "VenueError",
    "ConflictError",
    "NotFoundError",
    "StateError",
    "ValidationError",
]
