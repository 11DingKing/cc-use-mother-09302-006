"""领域事件定义、序列化与幂等键。

所有状态变更都先表达为事件，再由聚合应用。事件一经追加即不可变；
需要"撤回/取消"时追加新的补偿事件，而不是修改历史事件。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Mapping

# 事件类型常量
CLASS_REGISTERED = "class_registered"
SESSION_SCHEDULED = "session_scheduled"
STUDENT_REGISTERED = "student_registered"
ACCOMMODATION_APPROVED = "accommodation_approved"
CONSENT_GRANTED = "consent_granted"
CONSENT_WITHDRAWN = "consent_withdrawn"
SIGNED_UP = "signed_up"
SIGNUP_CANCELLED = "signup_cancelled"
ROSTER_PUBLISHED = "roster_published"
CAPACITY_EXPANDED = "capacity_expanded"
ATTENDANCE_MARKED = "attendance_marked"
NOTIFICATION_RECORDED = "notifications_recorded"
TRANSFER_OFFERED = "transfer_offered"
TRANSFER_ACCEPTED = "transfer_accepted"
TRANSFER_DECLINED = "transfer_declined"
SESSION_CLOSED = "session_closed"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().isoformat(timespec="microseconds")


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class Event:
    """一条不可变领域事件。

    sequence 由存储层在事务内分配；event_id 对客户端可见，用于去重。
    idempotency_key 为 None 表示该事件不参与命令级幂等。
    """

    event_id: str
    event_type: str
    aggregate_id: str
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int | None = None
    idempotency_key: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "payload_json": json.dumps(self.payload, ensure_ascii=False, sort_keys=True),
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Event":
        return cls(
            event_id=row["event_id"],
            event_type=row["event_type"],
            aggregate_id=row["aggregate_id"],
            occurred_at=row["occurred_at"],
            payload=json.loads(row["payload_json"]),
            sequence=row["sequence"],
            idempotency_key=row["idempotency_key"],
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
