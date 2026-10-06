"""时间与缓冲工具。

时间一律使用带时区的 ``datetime``，区间为左闭右开 ``[start, end)``，
因此跨日（含跨多日）活动只是普通的长区间，无需特殊分支。
"""
from __future__ import annotations

from datetime import datetime, timedelta

ISO_FORMAT = "iso8601"


def parse_dt(value: datetime | str) -> datetime:
    """接受 ``datetime`` 或 ISO 字符串，要求显式时区。"""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"无法解析时间：{value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"时间必须带时区：{value!r}")
    return dt


def fmt(dt: datetime) -> str:
    return dt.isoformat()


def as_interval(value: dict | None, prefix: str = "") -> tuple[str, str] | None:
    """序列化时间区间。"""
    if value is None:
        return None
    return fmt(value[prefix + "start"]), fmt(value[prefix + "end"])


def overlap(a_start: datetime, a_end: datetime,
            b_start: datetime, b_end: datetime) -> bool:
    """左闭右开区间是否相交。"""
    return a_start < b_end and b_start < a_end


def intersection(a_start: datetime, a_end: datetime,
                 b_start: datetime, b_end: datetime) -> tuple[datetime, datetime]:
    return max(a_start, b_start), min(a_end, b_end)


def expand(start: datetime, end: datetime,
           pre_minutes: int, post_minutes: int) -> tuple[datetime, datetime]:
    """把占用区间向前后扩展转换/清场缓冲。"""
    return (
        start - timedelta(minutes=pre_minutes),
        end + timedelta(minutes=post_minutes),
    )
