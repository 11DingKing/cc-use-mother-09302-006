"""SQLite 事件存储。

- 所有写操作在单个 ``BEGIN IMMEDIATE`` 事务内完成：重放 -> 校验命令 ->
  追加事件，保证原子递补、跨场转移等多事件命令要么全成要么全不成。
- ``(command_type, idempotency_key)`` 唯一约束实现命令级幂等：重放相同
  事件（携带相同幂等键）直接返回首次结果，绝不二次占位。
- 纯标准库实现，默认开启 WAL 与外键约束。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from .events import Event, parse_ts, utcnow
from .model import State, apply, handle

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS idempotency (
    command_type TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    event_ids_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_sequence INTEGER NOT NULL,
    PRIMARY KEY (command_type, idempotency_key)
);
"""


class EventStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
            isolation_level=None,  # 手动事务
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ 重放

    def load_state(self, up_to_sequence: int | None = None) -> State:
        """重放全部事件（或到指定序号）构建投影。读事务快照隔离。"""
        state = State()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                sql = "SELECT * FROM events ORDER BY sequence ASC"
                params: tuple = ()
                if up_to_sequence is not None:
                    sql += " WHERE sequence <= ?"
                    params = (up_to_sequence,)
                rows = self._conn.execute(sql, params).fetchall()
            finally:
                self._conn.execute("COMMIT")
        for row in rows:
            apply(
                state,
                row["event_type"],
                row["aggregate_id"],
                json.loads(row["payload_json"]),
                parse_ts(row["occurred_at"]),
            )
        return state

    def list_events(self, aggregate_id: str | None = None) -> list[Event]:
        with self._lock:
            if aggregate_id:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE aggregate_id = ? ORDER BY sequence ASC",
                    (aggregate_id,),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM events ORDER BY sequence ASC").fetchall()
        return [Event.from_row(row) for row in rows]

    # ------------------------------------------------------------ 写入

    def execute(
        self,
        command_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
        response_builder: Callable[[list[Event]], dict] | None = None,
    ) -> dict[str, Any]:
        """原子执行一条命令，返回可序列化结果。

        重复的 (command_type, idempotency_key) 返回首次响应并带
        ``replayed=true``，不产生任何新事件。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    row = self._conn.execute(
                        "SELECT response_json FROM idempotency WHERE command_type=? AND idempotency_key=?",
                        (command_type, idempotency_key),
                    ).fetchone()
                    if row is not None:
                        self._conn.execute("COMMIT")
                        result = json.loads(row["response_json"])
                        result["replayed"] = True
                        return result

                state = self.load_state_locked()
                at = utcnow()
                produced = handle(state, command_type, payload, at)

                events: list[Event] = []
                for event_type, aggregate_id, event_payload in produced:
                    event_id = str(uuid.uuid4())
                    occurred_at = at.isoformat(timespec="microseconds")
                    cur = self._conn.execute(
                        """INSERT INTO events(event_id, event_type, aggregate_id, occurred_at,
                                              payload_json, idempotency_key)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            event_id,
                            event_type,
                            aggregate_id,
                            occurred_at,
                            json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                            idempotency_key,
                        ),
                    )
                    events.append(
                        Event(
                            event_id=event_id,
                            event_type=event_type,
                            aggregate_id=aggregate_id,
                            occurred_at=occurred_at,
                            payload=event_payload,
                            sequence=cur.lastrowid,
                            idempotency_key=idempotency_key,
                        )
                    )

                response = response_builder(events) if response_builder else {
                    "command": command_type,
                    "events": [
                        {
                            "event_id": e.event_id,
                            "event_type": e.event_type,
                            "aggregate_id": e.aggregate_id,
                            "sequence": e.sequence,
                            "payload": e.payload,
                        }
                        for e in events
                    ],
                    "event_ids": [e.event_id for e in events],
                }

                if idempotency_key is not None:
                    self._conn.execute(
                        """INSERT INTO idempotency(command_type, idempotency_key,
                                                    event_ids_json, response_json, created_sequence)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            command_type,
                            idempotency_key,
                            json.dumps([e.event_id for e in events]),
                            json.dumps(response, ensure_ascii=False, sort_keys=True),
                            events[-1].sequence if events else 0,
                        ),
                    )
                self._conn.execute("COMMIT")
                response.setdefault("replayed", False)
                return response
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def load_state_locked(self) -> State:
        """在已持有写事务时重放（不加新事务）。"""
        state = State()
        rows = self._conn.execute("SELECT * FROM events ORDER BY sequence ASC").fetchall()
        for row in rows:
            apply(
                state,
                row["event_type"],
                row["aggregate_id"],
                json.loads(row["payload_json"]),
                parse_ts(row["occurred_at"]),
            )
        return state
