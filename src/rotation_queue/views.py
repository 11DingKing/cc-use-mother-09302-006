"""读取模型与基于角色的隐私裁剪。

角色边界：
- 班主任（teacher）：可看排序理由（因素编码 + 人话解释）、名额状态、
  通知送达状态；看不到监护人身份/联系方式、照顾依据证据编号与医疗细节。
- 活动志愿者（volunteer）：只能看待签到名单与签到状态，看不到排序理由
  与任何照顾信息。
- 学生（student）：只能看自己的名次、理由与本人通知状态。
"""
from __future__ import annotations

from .events import utcnow
from .model import State, Session
from .ranking import build_ranking, factor_text, rank_key

# 教师端可见的因素白名单（其余字段一律不出现在响应里）
TEACHER_FACTOR_KEYS = {
    "history_count",
    "accommodation_level",
    "enqueued_at",
    "transferred_in",
    "tie_breaker_student_id",
    "tied_with_previous",
}

ACCOMMODATION_LABELS = {
    0: "医疗类（经批准）",
    1: "无障碍类（经批准）",
    2: "其他经批准依据",
    None: "无",
    9: "无",
}


def _latest_notification(session: Session, student_id: str) -> dict | None:
    found = None
    for notif in session.notifications.values():
        if notif.student_id == student_id:
            found = notif  # dict 保持插入顺序，取最后一条（业务上 promoted 最晚）
    if found is None:
        return None
    return {
        "notification_id": found.notification_id,
        "kind": found.kind,
        "delivered": found.delivered,
    }


def _all_notifications(session: Session, student_id: str) -> list[dict]:
    """该生在本场的全部通知（含历史候补/递补），供教师逐条上报回执。"""
    return [
        {
            "notification_id": n.notification_id,
            "kind": n.kind,
            "delivered": n.delivered,
        }
        for n in session.notifications.values()
        if n.student_id == student_id
    ]


def _redact_factors(factors: dict) -> dict:
    redacted = {k: v for k, v in factors.items() if k in TEACHER_FACTOR_KEYS}
    if "accommodation_level" in redacted:
        level = redacted["accommodation_level"]
        redacted["accommodation_label"] = ACCOMMODATION_LABELS.get(level, "无")
    return redacted


def _all_roster_entries(session: Session) -> list[dict]:
    """合并发布时快照与之后的递补记录（均为不可变历史）。"""
    return list(session.roster_snapshot) + [dict(promo) for promo in session.promotions]


def _stored_entry_map(session: Session) -> dict[str, dict]:
    """发布快照与递补记录（不可变历史），按学生折叠，递补行覆盖候补行。"""
    by_student: dict[str, dict] = {}
    for entry in _all_roster_entries(session):
        by_student[entry["student_id"]] = entry
    return by_student


def teacher_session_view(state: State, session_id: str) -> dict:
    session = state.require_session(session_id)

    # 发布前：名单尚未冻结，按当前快照实时计算，入选/候补标记随报名变化
    if session.status == "scheduled":
        live = [state.signup_view(session, sid, utcnow()) for sid in session.signups]
        ranked = build_ranking(live, session.capacity)
        rows = []
        for entry in ranked:
            student = state.students.get(entry.student_id)
            rows.append({
                "student_id": entry.student_id,
                "name": student.name if student else None,
                "position": entry.position,
                "selected": entry.selected,
                "active": True,
                "attended": False,
                "reason_text": entry.reason_text,
                "reason_codes": entry.reason_codes,
                "factors": _redact_factors(entry.factors),
                "notification": None,
            })
        return {
            "session_id": session.session_id,
            "class_id": session.class_id,
            "starts_at": session.starts_at.isoformat(),
            "capacity": session.capacity,
            "status": session.status,
            "selected_count": len(session.selected),
            "attended_count": 0,
            "roster": rows,
        }

    stored = _stored_entry_map(session)

    # 1) 入选者：按权威入选顺序（发布顺序 + 逐次递补追加），理由取落库快照
    rows: list[dict] = []
    for display_pos, sid in enumerate(session.selected, start=1):
        item = dict(stored.get(sid, {}))
        student = state.students.get(sid)
        rows.append({
            "student_id": sid,
            "name": student.name if student else None,
            "position": display_pos,
            "selected": True,
            "active": sid in session.signups,
            "attended": sid in session.attended,
            "reason_text": item.get("reason_text"),
            "reason_codes": item.get("reason_codes", []),
            "factors": _redact_factors(item["factors"]) if item.get("factors") else None,
            "notification": _latest_notification(session, sid),
            "notifications": _all_notifications(session, sid),
        })

    # 2) 候补者：当前仍在报名池中但未入选，按当前状态实时确定性重算
    live_waitlist = [
        state.signup_view(session, sid, utcnow())
        for sid in session.signups
        if sid not in session.selected
    ]
    live_waitlist.sort(key=rank_key)
    base = len(session.selected)
    prev_business = None
    for offset, view_ in enumerate(live_waitlist):
        student = state.students.get(view_.student_id)
        codes, text = factor_text(view_)
        business = rank_key(view_)[:-1]
        tied = prev_business is not None and prev_business == business
        if tied:
            text += "；与前一名业务条件相同，按学生编号决胜"
        text += f"；当前候补第 {offset + 1} 位（总名次 {base + offset + 1}），名额空出时按此顺序原子递补"
        rows.append({
            "student_id": view_.student_id,
            "name": student.name if student else None,
            "position": base + offset + 1,
            "selected": False,
            "active": True,
            "attended": False,
            "reason_text": text,
            "reason_codes": codes,
            "factors": _redact_factors({
                "history_count": view_.history_count,
                "accommodation_level": view_.accommodation_level,
                "enqueued_at": view_.signed_up_at.isoformat(),
                "transferred_in": view_.transferred_in,
                "tie_breaker_student_id": view_.student_id,
                "tied_with_previous": tied,
            }),
            "notification": _latest_notification(session, view_.student_id),
            "notifications": _all_notifications(session, view_.student_id),
        })
        prev_business = business

    # 3) 已退出但留有发布快照的学生：保留可审计行，置底，不参与当前名次
    for sid, item in stored.items():
        if sid in session.signups or sid in session.selected:
            continue
        student = state.students.get(sid)
        rows.append({
            "student_id": sid,
            "name": student.name if student else None,
            "position": None,
            "selected": False,
            "active": False,
            "attended": sid in session.attended,
            "reason_text": item.get("reason_text"),
            "reason_codes": item.get("reason_codes", []),
            "factors": _redact_factors(item["factors"]) if item.get("factors") else None,
            "notification": _latest_notification(session, sid),
            "notifications": _all_notifications(session, sid),
        })

    rows.sort(key=lambda e: (
        0 if e["selected"] else (1 if e["active"] else 2),
        e["position"] if e["position"] is not None else 10**9,
        e["student_id"],
    ))
    return {
        "session_id": session.session_id,
        "class_id": session.class_id,
        "starts_at": session.starts_at.isoformat(),
        "capacity": session.capacity,
        "status": session.status,
        "selected_count": len(session.selected),
        "attended_count": len(session.attended),
        "roster": rows,
    }


def volunteer_checkin_view(state: State, session_id: str) -> dict:
    """志愿者：仅签到用途的最小信息。"""
    session = state.require_session(session_id)
    rows = []
    for sid in session.selected:
        student = state.students.get(sid)
        rows.append({
            "student_id": sid,
            "name": student.name if student else None,
            "attended": sid in session.attended,
        })
    return {
        "session_id": session.session_id,
        "status": session.status,
        "capacity": session.capacity,
        "selected_count": len(session.selected),
        "attended_count": len(session.attended),
        "attendees": rows,
    }


def student_session_view(state: State, session_id: str, student_id: str) -> dict:
    session = state.require_session(session_id)
    state.require_student(student_id)
    full = teacher_session_view(state, session_id)
    for row in full["roster"]:
        if row["student_id"] == student_id:
            return {
                "session_id": session.session_id,
                "status": session.status,
                "entry": {k: row[k] for k in (
                    "student_id", "name", "position", "selected", "active",
                    "attended", "reason_text", "reason_codes", "factors", "notification",
                )},
            }
    # 名单未发布或学生尚未进入任何快照：只返回报名状态，不泄露他人信息
    return {
        "session_id": session.session_id,
        "status": session.status,
        "entry": {
            "student_id": student_id,
            "signed_up": student_id in session.signups,
        },
    }


def preview_roster(state: State, session_id: str, at) -> dict:
    """发布前班主任预览：用当前快照实时计算确定性排序（不落库）。"""
    session = state.require_session(session_id)
    views = [state.signup_view(session, sid, at) for sid in session.signups]
    ranking = build_ranking(views, session.capacity)
    return {
        "session_id": session.session_id,
        "capacity": session.capacity,
        "status": session.status,
        "roster": [
            {
                "student_id": e.student_id,
                "name": state.students[e.student_id].name,
                "position": e.position,
                "selected": e.selected,
                "reason_text": e.reason_text,
                "reason_codes": e.reason_codes,
                "factors": _redact_factors(e.factors),
            }
            for e in ranking
        ],
    }


def session_overview(state: State, session_id: str) -> dict:
    """不含任何人名与理由的场次概览（供调度/排期使用）。"""
    session = state.require_session(session_id)
    undelivered = sum(
        1 for n in session.notifications.values() if n.delivered is False
    )
    pending = sum(1 for n in session.notifications.values() if n.delivered is None)
    return {
        "session_id": session.session_id,
        "class_id": session.class_id,
        "starts_at": session.starts_at.isoformat(),
        "capacity": session.capacity,
        "status": session.status,
        "signup_count": len(session.signups),
        "selected_count": len(session.selected),
        "attended_count": len(session.attended),
        "notifications_total": len(session.notifications),
        "notifications_pending": pending,
        "notifications_undelivered": undelivered,
    }
