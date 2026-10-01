"""HTTP 服务入口：python3 -m rotation_queue.server [--db data/queue.db] [--port 8080]"""
from __future__ import annotations

import argparse

from .api import build_server
from .service import QueueService
from .store import EventStore


def main() -> None:
    parser = argparse.ArgumentParser(description="学生互动轮转排队后端")
    parser.add_argument("--db", default="data/queue.db", help="SQLite 数据库路径（默认 data/queue.db）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    store = EventStore(args.db)
    service = QueueService(store)
    httpd = build_server(service, host=args.host, port=args.port)
    print(f"轮转排队服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
