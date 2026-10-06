"""示例数据：可拆分展厅与两套布局。

层级::

    科技馆(venue)
      1号馆(H1)
        中央展厅(H1-MAIN)  buffer 30 分钟
          隔断东区(H1-E)
          隔断西区(H1-W)

布局::

    L-open  全开布局：1 个区域 Z-whole（容量 600，舞台/投影/同传）
    L-split 拆分布局：2 个区域 Z-east（300，投影）、Z-west（260，音响/同传）
"""
from __future__ import annotations

from .service import VenueService


def build_demo_service() -> VenueService:
    svc = VenueService()

    svc.create_space({"space_id": "venue", "name": "科技馆"})
    svc.create_space({"space_id": "H1", "name": "1号馆", "parent_id": "venue"})
    svc.create_space({
        "space_id": "H1-MAIN", "name": "中央展厅",
        "parent_id": "H1", "buffer_minutes": 30,
    })
    svc.create_space({
        "space_id": "H1-E", "name": "中央展厅-隔断东区",
        "parent_id": "H1-MAIN", "buffer_minutes": 30,
    })
    svc.create_space({
        "space_id": "H1-W", "name": "中央展厅-隔断西区",
        "parent_id": "H1-MAIN", "buffer_minutes": 30,
    })

    svc.create_layout({
        "layout_id": "L-open", "space_id": "H1-MAIN", "name": "全开布局",
        "zones": [{
            "zone_id": "Z-whole", "name": "全展厅",
            "space_id": "H1-MAIN", "capacity": 600,
            "equipment": ["舞台", "投影", "音响", "同传"],
        }],
    })
    svc.create_layout({
        "layout_id": "L-split", "space_id": "H1-MAIN", "name": "隔断拆分布局",
        "zones": [
            {
                "zone_id": "Z-east", "name": "隔断东区",
                "space_id": "H1-E", "capacity": 300,
                "equipment": ["投影", "音响"],
            },
            {
                "zone_id": "Z-west", "name": "隔断西区",
                "space_id": "H1-W", "capacity": 260,
                "equipment": ["音响", "同传"],
            },
        ],
    })
    return svc
