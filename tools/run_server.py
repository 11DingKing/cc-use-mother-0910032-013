"""启动本地 API 服务（加载演示数据）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from venue_capacity.api import main

if __name__ == "__main__":
    main()
