"""命令行入口：``python -m venue_scheduling.serve [--port 8000] [--seed]``。"""
from __future__ import annotations

import argparse

from .api import serve
from .seed import build_demo_service


def main() -> None:
    parser = argparse.ArgumentParser(description="场地容量冲突治理后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="载入可拆分展厅示例数据")
    args = parser.parse_args()
    service = build_demo_service() if args.seed else None
    serve(args.host, args.port, service)


if __name__ == "__main__":
    main()
