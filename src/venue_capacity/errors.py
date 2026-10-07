"""服务层异常类型。"""
from __future__ import annotations


class NotFoundError(LookupError):
    """资源不存在。"""


class ValidationError(ValueError):
    """输入不合法。"""


class StateError(RuntimeError):
    """当前状态不允许该操作。"""


class ConflictError(RuntimeError):
    """预约冲突：携带冲突路径与可行替代空间。"""

    def __init__(self, conflicts: list[dict], alternatives: list[dict] | None = None) -> None:
        self.conflicts = conflicts
        self.alternatives = alternatives or []
        paths = "、".join(dict.fromkeys(c.get("path", "?") for c in conflicts))
        super().__init__(f"预约冲突：{paths}")

    def to_dict(self) -> dict:
        return {
            "error": "conflict",
            "message": str(self),
            "conflicts": self.conflicts,
            "alternatives": self.alternatives,
        }
