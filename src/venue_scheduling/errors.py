"""领域错误。

冲突类错误携带结构化载荷（冲突路径、需求不满足项、可行替代空间），
由 Web 层原样返回给调用方。
"""
from __future__ import annotations

from typing import Any


class VenueError(Exception):
    """所有领域错误的基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.payload = payload or {}

    def to_dict(self) -> dict[str, Any]:
        result = {"error": self.code, "message": self.message}
        result.update(self.payload)
        return result


class ValidationError(VenueError):
    status = 400
    code = "invalid"


class NotFoundError(VenueError):
    status = 404
    code = "not_found"


class StateError(VenueError):
    status = 409
    code = "illegal_state"


class ConflictError(VenueError):
    """预约/切换/关闭与既有占用冲突，409 返回完整解释。"""

    status = 409
    code = "conflict"
