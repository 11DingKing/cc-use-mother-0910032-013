"""演示数据：可拆分综合展厅（东区/西区）与多功能室。"""
from __future__ import annotations

from .service import VenueService


def build_demo_service() -> VenueService:
    service = VenueService()
    service.add_space("venue", "市民文化中心")
    service.add_space("hall", "综合展厅", parent_id="venue")
    service.add_space("hall-east", "东区", parent_id="hall")
    service.add_space("hall-west", "西区", parent_id="hall")
    service.add_space("studio", "多功能室", parent_id="venue")

    service.add_layout(
        "hall-full", "hall", "全厅开放", 500, {"stage", "projector", "sound"},
        conversion_minutes=60,
    )
    service.add_layout(
        "east-theatre", "hall-east", "剧场式", 200, {"projector", "sound"},
        conversion_minutes=30,
    )
    service.add_layout(
        "east-banquet", "hall-east", "宴会式", 150, {"table", "sound"},
        conversion_minutes=45,
    )
    service.add_layout(
        "west-theatre", "hall-west", "剧场式", 180, {"projector"},
        conversion_minutes=30,
    )
    service.add_layout(
        "west-expo", "hall-west", "展览式", 100, {"booth"},
        conversion_minutes=20,
    )
    service.add_layout(
        "studio-class", "studio", "课堂式", 80, {"projector", "mic"},
        conversion_minutes=15,
    )
    return service
