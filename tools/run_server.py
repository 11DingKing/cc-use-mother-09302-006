"""启动学生互动轮转排队后端 HTTP 服务。

用法：python3 tools/run_server.py [db_path] [port]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation.api import serve
from rotation.service import RotationService

if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "rotation.db")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    print(f"轮转排队后端监听 http://127.0.0.1:{port}（数据文件：{db_path}）")
    serve(RotationService(db_path), port=port)
