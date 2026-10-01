"""应用服务：编排命令执行与读取视图，供 API/CLI 复用。

每个写方法返回 (events, state) 之后的视图数据；调用方（HTTP 层）
负责角色鉴权。幂等键由调用方透传，重复提交不会二次占位。
"""
from __future__ import annotations

from typing import Any

from .events import utcnow
from .store import EventStore
from . import views as read_views


class QueueService:
    def __init__(self, store: EventStore) -> None:
        self.store = store

    # ---------------------------------------------------------- 基础数据

    def register_class(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("register_class", payload, idem_key)
        return {"class_id": payload["class_id"]}

    def register_student(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("register_student", payload, idem_key)
        return {"student_id": payload["student_id"]}

    def schedule_session(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("schedule_session", payload, idem_key)
        return {"session_id": payload["session_id"]}

    # ---------------------------------------------------------- 合规

    def approve_accommodation(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("approve_accommodation", payload, idem_key)
        # 不回传任何证据/医疗细节
        return {"student_id": payload["student_id"], "case_id": payload["case_id"], "status": "approved"}

    def grant_consent(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("grant_consent", payload, idem_key)
        return {"student_id": payload["student_id"], "consent": "active"}

    def withdraw_consent(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("withdraw_consent", payload, idem_key)
        cancelled = [
            e["aggregate_id"]
            for e in result.get("events", [])
            if e["event_type"] == "signup_cancelled"
        ]
        return {
            "student_id": payload["student_id"],
            "consent": "withdrawn",
            "removed_or_promoted_in_sessions": sorted(set(cancelled)),
            "replayed": result.get("replayed", False),
        }

    # ---------------------------------------------------------- 报名/名单

    def sign_up(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("sign_up", payload, idem_key)
        state = self.store.load_state()
        session = state.sessions[payload["session_id"]]
        sid = payload["student_id"]
        selected = sid in session.selected
        return {
            "session_id": payload["session_id"],
            "student_id": sid,
            "signed_up": sid in session.signups,
            "selected": selected,
            "position": session.selected.index(sid) + 1 if selected else None,
            "replayed": result.get("replayed", False),
        }

    def cancel_signup(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("cancel_signup", payload, idem_key)
        return {"session_id": payload["session_id"], "student_id": payload["student_id"],
                "cancelled": True, "replayed": result.get("replayed", False)}

    def preview_roster(self, session_id: str) -> dict:
        state = self.store.load_state()
        return read_views.preview_roster(state, session_id, utcnow())

    def publish_roster(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("publish_roster", payload, idem_key)
        state = self.store.load_state()
        view = read_views.teacher_session_view(state, payload["session_id"])
        return {
            "session_id": payload["session_id"],
            "capacity": view["capacity"],
            "selected_count": view["selected_count"],
            "roster": view["roster"],
            "replayed": result.get("replayed", False),
        }

    def expand_capacity(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("expand_capacity", payload, idem_key)
        promotions: list[dict] = []
        for e in result.get("events", []):
            promotions.extend(e["payload"].get("promotions", []))
        return {
            "session_id": payload["session_id"],
            "capacity": payload["new_capacity"],
            "promoted": [
                {"student_id": p["student_id"], "position": p["position"],
                 "reason_text": p.get("reason_text", "")}
                for p in promotions
            ],
            "replayed": result.get("replayed", False),
        }

    # ---------------------------------------------------------- 签到

    def mark_attendance(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("mark_attendance", payload, idem_key)
        return {"session_id": payload["session_id"], "student_id": payload["student_id"],
                "attended": True, "replayed": result.get("replayed", False)}

    # ---------------------------------------------------------- 转移

    def offer_transfer(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("offer_transfer", payload, idem_key)
        return {"offer_id": payload["offer_id"], "status": "pending"}

    def accept_transfer(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("accept_transfer", payload, idem_key)
        return {"offer_id": payload["offer_id"], "status": "accepted",
                "replayed": result.get("replayed", False)}

    def decline_transfer(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("decline_transfer", payload, idem_key)
        return {"offer_id": payload["offer_id"], "status": "declined"}

    # ---------------------------------------------------------- 通知

    def record_notifications(self, payload: dict, idem_key: str | None = None) -> dict:
        result = self.store.execute("record_notifications", payload, idem_key)
        return {"session_id": payload["session_id"], "recorded": len(payload["receipts"]),
                "replayed": result.get("replayed", False)}

    def close_session(self, payload: dict, idem_key: str | None = None) -> dict:
        self.store.execute("close_session", payload, idem_key)
        return {"session_id": payload["session_id"], "status": "closed"}

    # ---------------------------------------------------------- 读取

    def teacher_view(self, session_id: str) -> dict:
        return read_views.teacher_session_view(self.store.load_state(), session_id)

    def volunteer_view(self, session_id: str) -> dict:
        return read_views.volunteer_checkin_view(self.store.load_state(), session_id)

    def student_view(self, session_id: str, student_id: str) -> dict:
        return read_views.student_session_view(self.store.load_state(), session_id, student_id)

    def overview(self, session_id: str) -> dict:
        return read_views.session_overview(self.store.load_state(), session_id)
