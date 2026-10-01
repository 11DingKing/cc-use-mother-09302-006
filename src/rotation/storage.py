"""SQLite 存储层：表结构、连接与事务助手。

- 每个命令一个连接、一个 BEGIN IMMEDIATE 事务，写操作天然串行，
  “同时签到/同时报名”等并发场景由数据库保证确定结果；
- entries 上的部分唯一索引保证同一学生在同一场次最多一条有效记录，
  是“重放不重复占位”的数据库兜底（第一道防线是 events 幂等表）。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    student_id       TEXT PRIMARY KEY,
    class_id         TEXT NOT NULL,
    name             TEXT NOT NULL,
    guardian_contact TEXT,               -- 敏感：仅管理侧可见，不出现在任何队列视图
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    class_id   TEXT NOT NULL,            -- 主办班级
    title      TEXT NOT NULL,
    capacity   INTEGER NOT NULL CHECK (capacity >= 0),
    starts_at  TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_eligible_classes (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    class_id   TEXT NOT NULL,
    PRIMARY KEY (session_id, class_id)
);

CREATE TABLE IF NOT EXISTS consents (
    student_id TEXT PRIMARY KEY REFERENCES students(student_id),
    status     TEXT NOT NULL CHECK (status IN ('active', 'withdrawn')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS care_grants (
    grant_id     TEXT PRIMARY KEY,
    student_id   TEXT NOT NULL REFERENCES students(student_id),
    tier         INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 9),
    detail       TEXT NOT NULL,          -- 敏感：照顾依据说明，仅审批人可见
    status       TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'revoked')),
    valid_until  TEXT,                   -- 为空表示长期有效
    submitted_at TEXT NOT NULL,
    decided_at   TEXT,
    decided_by   TEXT
);

CREATE TABLE IF NOT EXISTS entries (
    entry_id          TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions(session_id),
    student_id        TEXT NOT NULL REFERENCES students(student_id),
    status            TEXT NOT NULL CHECK (status IN
        ('queued', 'offered', 'checked_in', 'completed', 'no_show', 'withdrawn', 'removed')),
    registered_at     TEXT NOT NULL,     -- 原始报名时间，跨场转移时保留
    transferred_from  TEXT,
    offered_at        TEXT,
    offer_seq         INTEGER,           -- 本场次内获得名额的先后顺序（单调）
    offer_explanation TEXT,              -- 获得名额那一刻的排序理由快照（JSON）
    checked_in_at     TEXT,
    exit_reason       TEXT,
    updated_at        TEXT NOT NULL
);

-- 同一学生在同一场次最多一条有效记录：重放/并发都不会重复占位
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_active
    ON entries (session_id, student_id)
    WHERE status IN ('queued', 'offered', 'checked_in');
CREATE INDEX IF NOT EXISTS idx_entries_student ON entries (student_id, status);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries (session_id, status);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    entry_id        TEXT NOT NULL REFERENCES entries(entry_id),
    kind            TEXT NOT NULL,       -- offer：获得名额（含递补、扩容、转移）
    channel         TEXT NOT NULL DEFAULT 'guardian_app',
    status          TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'delivered', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    detail          TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications (status);

-- 幂等事件日志：每个命令一条，seq 提供全量事件的全序
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    actor      TEXT,
    payload    TEXT NOT NULL,
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    """文件型 SQLite 数据库（WAL 模式），每次操作使用独立连接。"""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 保证并发命令串行生效。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()
