"""无服务器演示：占用展开、冲突路径与可行替代空间。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_capacity.demo import build_demo_service
from venue_capacity.errors import ConflictError
from venue_capacity.models import ActivityRequirement

CST = timezone(timedelta(hours=8))


def main() -> None:
    service = build_demo_service()

    # 东区 09:00-12:00 剧场式讲座
    service.create_booking(
        "DEMO-1", "科普讲座", "hall-east", "east-theatre",
        datetime(2026, 11, 2, 9, 0, tzinfo=CST), datetime(2026, 11, 2, 12, 0, tzinfo=CST),
        ActivityRequirement(capacity=120, equipment=frozenset({"projector"})),
    )
    service.submit("DEMO-1")
    service.confirm("DEMO-1")
    print("== 已确认预约的实际占用展开 ==")
    print(json.dumps(service.booking_view("DEMO-1")["occupancy"], ensure_ascii=False, indent=2))

    # 同时段整厅年会：与子区域冲突
    service.create_booking(
        "DEMO-2", "企业年会", "hall", "hall-full",
        datetime(2026, 11, 2, 10, 0, tzinfo=CST), datetime(2026, 11, 2, 17, 0, tzinfo=CST),
        ActivityRequirement(capacity=300, equipment=frozenset({"stage"})),
    )
    service.submit("DEMO-2")
    try:
        service.confirm("DEMO-2")
    except ConflictError as exc:
        print("\n== 整厅预约冲突（子区域已占用） ==")
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))

    # 东区 11:00-13:00 剧场式：与已占用的东区重叠，返回可行替代空间
    service.create_booking(
        "DEMO-3", "客户答谢宴", "hall-east", "east-theatre",
        datetime(2026, 11, 2, 11, 0, tzinfo=CST), datetime(2026, 11, 2, 13, 0, tzinfo=CST),
        ActivityRequirement(capacity=100, equipment=frozenset({"projector"})),
    )
    service.submit("DEMO-3")
    try:
        service.confirm("DEMO-3")
    except ConflictError as exc:
        print("\n== 重复排期冲突与可行替代空间 ==")
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
