"""事件溯源聚合：投影状态与命令处理（纯函数式，确定性）。

写入路径统一为：重放事件得到 State -> 校验命令 -> 生成一批事件 ->
单事务追加。跨场次转移等跨聚合操作在同一批事件内原子完成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .events import (
    ACCOMMODATION_APPROVED,
    ATTENDANCE_MARKED,
    CAPACITY_EXPANDED,
    CLASS_REGISTERED,
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    NOTIFICATION_RECORDED,
    ROSTER_PUBLISHED,
    SESSION_CLOSED,
    SESSION_SCHEDULED,
    SIGNUP_CANCELLED,
    SIGNED_UP,
    STUDENT_REGISTERED,
    TRANSFER_ACCEPTED,
    TRANSFER_DECLINED,
    TRANSFER_OFFERED,
    parse_ts,
)
from .ranking import (
    NO_ACCOMMODATION,
    SignupView,
    RankedEntry,
    build_ranking,
    level_for,
    rank_key,
)

# ---------------------------------------------------------------- 投影状态


@dataclass
class Accommodation:
    case_id: str
    reason_type: str
    valid_until: datetime | None
    evidence_ref: str
    approved_at: datetime


@dataclass
class Student:
    student_id: str
    class_id: str
    name: str
    consent_active: bool = False
    guardian: str | None = None
    accommodations: dict[str, Accommodation] = field(default_factory=dict)
    history_count: int = 0


@dataclass
class SignupInfo:
    student_id: str
    enqueued_at: datetime
    via_transfer: bool = False


@dataclass
class Notification:
    notification_id: str
    student_id: str
    kind: str  # selected | waitlisted | promoted
    delivered: bool | None = None
    recorded_at: datetime | None = None


@dataclass
class TransferOffer:
    offer_id: str
    from_session: str
    to_session: str
    student_id: str
    status: str = "pending"  # pending | accepted | declined
    created_at: datetime | None = None


@dataclass
class Session:
    session_id: str
    class_id: str
    starts_at: datetime
    capacity: int
    status: str = "scheduled"  # scheduled | published | closed
    signups: dict[str, SignupInfo] = field(default_factory=dict)
    selected: list[str] = field(default_factory=list)
    attended: set[str] = field(default_factory=set)
    notifications: dict[str, Notification] = field(default_factory=dict)
    offers: dict[str, TransferOffer] = field(default_factory=dict)
    roster_snapshot: list[dict] = field(default_factory=list)
    promotions: list[dict] = field(default_factory=list)
    notif_seq: int = 0


@dataclass
class State:
    classes: dict[str, dict] = field(default_factory=dict)
    students: dict[str, Student] = field(default_factory=dict)
    sessions: dict[str, Session] = field(default_factory=dict)

    # ---- 查询辅助 -------------------------------------------------

    def require_class(self, class_id: str) -> dict:
        try:
            return self.classes[class_id]
        except KeyError:
            raise NotFoundError("class_not_found", f"班级不存在：{class_id}", 404)

    def require_student(self, student_id: str) -> Student:
        try:
            return self.students[student_id]
        except KeyError:
            raise NotFoundError("student_not_found", f"学生不存在：{student_id}", 404)

    def require_session(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise NotFoundError("session_not_found", f"场次不存在：{session_id}", 404)

    def active_accommodation(self, student: Student, at: datetime) -> Accommodation | None:
        best_level = NO_ACCOMMODATION
        best: Accommodation | None = None
        for acc in student.accommodations.values():
            if acc.valid_until is not None and acc.valid_until < at:
                continue
            level = level_for(acc.reason_type)
            if level is not None and level < best_level:
                best_level = level
                best = acc
        return best

    def signup_view(self, session: Session, student_id: str, at: datetime) -> SignupView:
        student = self.require_student(student_id)
        info = session.signups[student_id]
        acc = self.active_accommodation(student, at)
        return SignupView(
            student_id=student_id,
            signed_up_at=info.enqueued_at,
            history_count=student.history_count,
            consent_active=student.consent_active,
            accommodation_level=level_for(acc.reason_type) if acc else None,
            accommodation_reason=acc.reason_type if acc else None,
            transferred_in=info.via_transfer,
        )

    def waitlist_views(self, session: Session, at: datetime) -> list[SignupView]:
        chosen = set(session.selected)
        return [
            self.signup_view(session, sid, at)
            for sid in session.signups
            if sid not in chosen
        ]


# ---------------------------------------------------------------- 事件应用


def apply(state: State, event_type: str, aggregate_id: str, payload: dict, occurred_at: datetime) -> None:
    """把一条事件确定性地应用到投影。"""
    if event_type == CLASS_REGISTERED:
        if aggregate_id in state.classes:
            raise ConflictError("class_exists", f"班级已存在：{aggregate_id}", 409)
        state.classes[aggregate_id] = {"class_id": aggregate_id, "name": payload["name"]}

    elif event_type == STUDENT_REGISTERED:
        if aggregate_id in state.students:
            raise ConflictError("student_exists", f"学生已存在：{aggregate_id}", 409)
        state.require_class(payload["class_id"])
        state.students[aggregate_id] = Student(
            student_id=aggregate_id,
            class_id=payload["class_id"],
            name=payload["name"],
        )

    elif event_type == SESSION_SCHEDULED:
        if aggregate_id in state.sessions:
            raise ConflictError("session_exists", f"场次已存在：{aggregate_id}", 409)
        state.require_class(payload["class_id"])
        state.sessions[aggregate_id] = Session(
            session_id=aggregate_id,
            class_id=payload["class_id"],
            starts_at=parse_ts(payload["starts_at"]),
            capacity=int(payload["capacity"]),
        )

    elif event_type == ACCOMMODATION_APPROVED:
        student = state.require_student(aggregate_id)
        if payload["case_id"] in student.accommodations:
            raise ConflictError("case_exists", "照顾依据编号已存在", 409)
        student.accommodations[payload["case_id"]] = Accommodation(
            case_id=payload["case_id"],
            reason_type=payload["reason_type"],
            valid_until=parse_ts(payload["valid_until"]) if payload.get("valid_until") else None,
            evidence_ref=payload.get("evidence_ref", ""),
            approved_at=occurred_at,
        )

    elif event_type == CONSENT_GRANTED:
        state.require_student(aggregate_id).consent_active = True
        state.students[aggregate_id].guardian = payload.get("guardian")

    elif event_type == CONSENT_WITHDRAWN:
        state.require_student(aggregate_id).consent_active = False

    elif event_type == SIGNED_UP:
        session = state.require_session(aggregate_id)
        sid = payload["student_id"]
        if sid in session.signups:
            raise ConflictError("already_signed_up", "学生已报名该场次", 409)
        session.signups[sid] = SignupInfo(
            student_id=sid,
            enqueued_at=parse_ts(payload["enqueued_at"]),
            via_transfer=bool(payload.get("via_transfer", False)),
        )
        _apply_promotions(state, session, payload.get("promotions", []), occurred_at)
        for item in payload.get("notifications", []):
            _put_notification(session, item, occurred_at)

    elif event_type == SIGNUP_CANCELLED:
        session = state.require_session(aggregate_id)
        sid = payload["student_id"]
        session.signups.pop(sid, None)
        if sid in session.selected:
            session.selected.remove(sid)
        _apply_promotions(state, session, payload.get("promotions", []), occurred_at)
        for item in payload.get("notifications", []):
            _put_notification(session, item, occurred_at)

    elif event_type == ROSTER_PUBLISHED:
        session = state.require_session(aggregate_id)
        session.status = "published"
        session.capacity = int(payload["capacity"])
        session.selected = [e["student_id"] for e in payload["ranking"] if e["selected"]]
        session.roster_snapshot = payload["ranking"]
        for item in payload.get("notifications", []):
            _put_notification(session, item, occurred_at)

    elif event_type == CAPACITY_EXPANDED:
        session = state.require_session(aggregate_id)
        session.capacity = int(payload["new_capacity"])
        _apply_promotions(state, session, payload.get("promotions", []), occurred_at)
        for item in payload.get("notifications", []):
            _put_notification(session, item, occurred_at)

    elif event_type == ATTENDANCE_MARKED:
        session = state.require_session(aggregate_id)
        sid = payload["student_id"]
        if sid in session.attended:
            raise ConflictError("already_checked_in", "学生已签到", 409)
        session.attended.add(sid)
        state.require_student(sid).history_count += 1

    elif event_type == NOTIFICATION_RECORDED:
        session = state.require_session(aggregate_id)
        for receipt in payload["receipts"]:
            notif = session.notifications.get(receipt["notification_id"])
            if notif is not None:
                notif.delivered = bool(receipt["delivered"])
                notif.recorded_at = occurred_at

    elif event_type == TRANSFER_OFFERED:
        session = state.require_session(aggregate_id)
        offer = TransferOffer(
            offer_id=payload["offer_id"],
            from_session=payload["from_session"],
            to_session=payload["to_session"],
            student_id=payload["student_id"],
            created_at=occurred_at,
        )
        session.offers[offer.offer_id] = offer

    elif event_type == TRANSFER_ACCEPTED:
        _, offer = _find_offer(state, payload["offer_id"])
        offer.status = "accepted"

    elif event_type == TRANSFER_DECLINED:
        _, offer = _find_offer(state, payload["offer_id"])
        offer.status = "declined"

    elif event_type == SESSION_CLOSED:
        state.require_session(aggregate_id).status = "closed"

    else:
        raise ValidationError("unknown_event", f"未知事件类型：{event_type}")


def _put_notification(session: Session, item: dict, occurred_at: datetime) -> None:
    session.notif_seq += 1
    session.notifications[item["notification_id"]] = Notification(
        notification_id=item["notification_id"],
        student_id=item["student_id"],
        kind=item["kind"],
    )


def _apply_promotions(state: State, session: Session, promotions: list[dict], occurred_at: datetime) -> None:
    for entry in promotions:
        sid = entry["student_id"]
        if sid not in session.selected:
            session.selected.append(sid)
        session.promotions.append(entry)


def _find_offer(state: State, offer_id: str) -> tuple[Session, TransferOffer]:
    for session in state.sessions.values():
        if offer_id in session.offers:
            return session, session.offers[offer_id]
    raise NotFoundError("offer_not_found", f"转移申请不存在：{offer_id}", 404)


# ---------------------------------------------------------------- 命令处理

ProducedEvent = tuple[str, str, dict]  # event_type, aggregate_id, payload


def _entry_payload(entry: RankedEntry) -> dict:
    return {
        "student_id": entry.student_id,
        "position": entry.position,
        "selected": entry.selected,
        "factors": entry.factors,
        "reason_codes": entry.reason_codes,
        "reason_text": entry.reason_text,
    }


def _new_notification(session: Session, student_id: str, kind: str) -> dict:
    session.notif_seq += 1
    nid = f"notif:{session.session_id}:{student_id}:{kind}:{session.notif_seq}"
    return {"notification_id": nid, "student_id": student_id, "kind": kind}


def _fill_vacancies(state: State, session: Session, at: datetime) -> tuple[list[dict], list[dict]]:
    """按确定性规则把候补者补满当前空缺席位，返回 (晋升条目, 通知)。

    已入选者位置保持不变；只在候补池内按统一排序键取人，所以临时递补、
    迟到报名、转入学生都走同一条公平规则。
    """
    promotions: list[dict] = []
    notifications: list[dict] = []
    views = state.waitlist_views(session, at)
    if not views:
        return promotions, notifications
    ordered = sorted(views, key=rank_key)
    vacant = session.capacity - len(session.selected)
    for pos, view in enumerate(ordered[: max(0, vacant)]):
        entry = RankedEntry(
            student_id=view.student_id,
            rank_key=rank_key(view),
            position=len(session.selected) + pos + 1,
            selected=True,
            factors={
                "history_count": view.history_count,
                "accommodation_level": view.accommodation_level,
                "enqueued_at": view.signed_up_at.isoformat(),
                "transferred_in": view.transferred_in,
                "tie_breaker_student_id": view.student_id,
            },
            reason_codes=[],
            reason_text=(
                f"原入选者空出席位，按统一排序在候补池中位列第 {pos + 1}，原子递补获得名额"
            ),
        )
        promotions.append(_entry_payload(entry))
        notifications.append(_new_notification(session, view.student_id, "promoted"))
    return promotions, notifications


def handle(state: State, command_type: str, payload: dict, at: datetime) -> list[ProducedEvent]:
    handler = _HANDLERS.get(command_type)
    if handler is None:
        raise ValidationError("unknown_command", f"未知命令：{command_type}")
    return handler(state, payload, at)


def _h_register_class(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    cid = p["class_id"]
    if cid in state.classes:
        raise ConflictError("class_exists", f"班级已存在：{cid}", 409)
    return [(CLASS_REGISTERED, cid, {"name": p["name"]})]


def _h_schedule_session(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    sid = p["session_id"]
    state.require_class(p["class_id"])
    if sid in state.sessions:
        raise ConflictError("session_exists", f"场次已存在：{sid}", 409)
    capacity = int(p["capacity"])
    if capacity < 1:
        raise ValidationError("bad_capacity", "容量必须为正整数")
    starts_at = p["starts_at"]
    parse_ts(starts_at)  # 校验格式
    return [(SESSION_SCHEDULED, sid, {"class_id": p["class_id"], "starts_at": starts_at, "capacity": capacity})]


def _h_register_student(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    sid = p["student_id"]
    state.require_class(p["class_id"])
    if sid in state.students:
        raise ConflictError("student_exists", f"学生已存在：{sid}", 409)
    return [(STUDENT_REGISTERED, sid, {"class_id": p["class_id"], "name": p["name"]})]


def _h_approve_accommodation(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    student = state.require_student(p["student_id"])
    case_id = p["case_id"]
    if case_id in student.accommodations:
        raise ConflictError("case_exists", f"照顾依据编号已存在：{case_id}", 409)
    reason_type = p.get("reason_type", "other")
    if reason_type not in ("medical", "accessibility", "other"):
        raise ValidationError("bad_reason_type", "照顾依据类型非法")
    valid_until = p.get("valid_until")
    if valid_until:
        parse_ts(valid_until)
    return [(ACCOMMODATION_APPROVED, p["student_id"], {
        "case_id": case_id,
        "reason_type": reason_type,
        "valid_until": valid_until,
        "evidence_ref": p.get("evidence_ref", ""),
    })]


def _h_grant_consent(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    student = state.require_student(p["student_id"])
    if student.consent_active:
        raise ConflictError("consent_active", "监护授权已处于有效状态", 409)
    return [(CONSENT_GRANTED, p["student_id"], {"guardian": p.get("guardian")})]


def _h_withdraw_consent(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    student = state.require_student(p["student_id"])
    if not student.consent_active:
        raise ConflictError("consent_inactive", "监护授权已撤回或从未授予", 409)
    events: list[ProducedEvent] = [(CONSENT_WITHDRAWN, p["student_id"], {"reason": p.get("reason", "")})]
    # 授权撤回即时生效：从所有未关闭场次移除并原子递补，全部在同一事务内。
    for session in state.sessions.values():
        if session.status == "closed" or p["student_id"] not in session.signups:
            continue
        cancel = _cancel_signup_events(
            state, session, p["student_id"], at, reason="consent_withdrawn"
        )
        events.extend(cancel)
    return events


def _h_sign_up(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    student = state.require_student(p["student_id"])
    if session.status == "closed":
        raise ConflictError("session_closed", "场次已关闭", 409)
    if student.class_id != session.class_id:
        raise ValidationError("class_mismatch", "学生不属于该场次的班级")
    if not student.consent_active:
        raise ValidationError("no_consent", "监护授权无效，不能报名")
    if p["student_id"] in session.signups:
        raise ConflictError("already_signed_up", "学生已报名该场次", 409)
    enqueued_at = p.get("enqueued_at") or at.isoformat()
    parse_ts(enqueued_at)
    payload: dict = {"student_id": p["student_id"], "enqueued_at": enqueued_at}
    # 先在投影上登记，再让候补填补规则统一决定是否立即入选（发布后迟到报名同样参与公平排序）
    session.signups[p["student_id"]] = SignupInfo(
        student_id=p["student_id"], enqueued_at=parse_ts(enqueued_at)
    )
    if session.status == "published":
        promotions, notifications = _fill_vacancies(state, session, at)
        payload["promotions"] = promotions
        payload["notifications"] = notifications
    return [(SIGNED_UP, p["session_id"], payload)]


def _cancel_signup_events(state: State, session: Session, student_id: str, at: datetime, reason: str) -> list[ProducedEvent]:
    """生成取消事件；若取消者已入选，同一事件内携带原子递补结果。"""
    was_selected = student_id in session.selected
    session.signups.pop(student_id, None)
    if student_id in session.selected:
        session.selected.remove(student_id)
    payload: dict = {"student_id": student_id, "reason": reason}
    if was_selected and session.status == "published":
        promotions, notifications = _fill_vacancies(state, session, at)
        payload["promotions"] = promotions
        payload["notifications"] = notifications
    return [(SIGNUP_CANCELLED, session.session_id, payload)]


def _h_cancel_signup(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    student = state.require_student(p["student_id"])
    if session.status == "closed":
        raise ConflictError("session_closed", "场次已关闭", 409)
    if p["student_id"] not in session.signups:
        raise NotFoundError("not_signed_up", "学生未报名该场次", 404)
    return _cancel_signup_events(state, session, p["student_id"], at, reason=p.get("reason", "cancelled"))


def _h_publish_roster(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    if session.status != "scheduled":
        raise ConflictError("roster_published", "名单只能发布一次", 409)
    views = [state.signup_view(session, sid, at) for sid in session.signups]
    ranking = build_ranking(views, session.capacity)
    payload_ranking = [_entry_payload(e) for e in ranking]
    notifications: list[dict] = []
    for entry in ranking:
        kind = "selected" if entry.selected else "waitlisted"
        notifications.append(_new_notification(session, entry.student_id, kind))
    payload = {
        "capacity": session.capacity,
        "ranking": payload_ranking,
        "notifications": notifications,
    }
    return [(ROSTER_PUBLISHED, p["session_id"], payload)]


def _h_expand_capacity(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    if session.status == "closed":
        raise ConflictError("session_closed", "场次已关闭", 409)
    new_capacity = int(p["new_capacity"])
    if new_capacity <= session.capacity:
        raise ValidationError("bad_capacity", f"临时扩容必须大于当前容量 {session.capacity}")
    payload: dict = {"old_capacity": session.capacity, "new_capacity": new_capacity}
    session.capacity = new_capacity
    if session.status == "published":
        promotions, notifications = _fill_vacancies(state, session, at)
        payload["promotions"] = promotions
        payload["notifications"] = notifications
    return [(CAPACITY_EXPANDED, p["session_id"], payload)]


def _h_mark_attendance(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    sid = p["student_id"]
    if session.status != "published":
        raise ConflictError("not_published", "名单未发布，不能签到", 409)
    if sid not in session.selected:
        raise ValidationError("not_on_roster", "候补者不能签到，请等待递补")
    if sid in session.attended:
        raise ConflictError("already_checked_in", "学生已签到，请勿重复操作", 409)
    return [(ATTENDANCE_MARKED, p["session_id"], {"student_id": sid})]


def _h_offer_transfer(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    src = state.require_session(p["from_session"])
    dst = state.require_session(p["to_session"])
    sid = p["student_id"]
    student = state.require_student(sid)
    if src.class_id != dst.class_id:
        raise ValidationError("class_mismatch", "只能在同班的场次间转移")
    if dst.status == "closed":
        raise ConflictError("target_closed", "目标场次已关闭", 409)
    if sid not in src.signups:
        raise ValidationError("not_in_source", "学生不在源场次中")
    if sid in dst.signups:
        raise ConflictError("already_in_target", "学生已在目标场次中", 409)
    if not student.consent_active:
        raise ValidationError("no_consent", "监护授权无效，不能转移")
    for offer in src.offers.values():
        if offer.student_id == sid and offer.to_session == dst.session_id and offer.status == "pending":
            raise ConflictError("offer_pending", "已存在待处理的转移申请", 409)
    offer_id = p["offer_id"]
    if any(offer_id in s.offers for s in state.sessions.values()):
        raise ConflictError("offer_exists", f"转移申请编号已存在：{offer_id}", 409)
    payload = {
        "offer_id": offer_id,
        "from_session": src.session_id,
        "to_session": dst.session_id,
        "student_id": sid,
    }
    return [(TRANSFER_OFFERED, src.session_id, payload)]


def _h_accept_transfer(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    _, offer = _find_offer(state, p["offer_id"])
    if offer.status != "pending":
        raise ConflictError("offer_decided", "转移申请已处理", 409)
    if p.get("student_id") and p["student_id"] != offer.student_id:
        raise ForbiddenError("not_offer_owner", "学生只能处理本人的转移申请", 403)
    sid = offer.student_id
    dst = state.require_session(offer.to_session)
    src = state.require_session(offer.from_session)
    student = state.require_student(sid)
    if dst.status == "closed":
        raise ConflictError("target_closed", "目标场次已关闭", 409)
    if sid in dst.signups:
        raise ConflictError("already_in_target", "学生已在目标场次中", 409)
    if not student.consent_active:
        raise ValidationError("no_consent", "监护授权无效，不能转移")
    events: list[ProducedEvent] = []
    # 1) 先捕获原报名时刻，源场次移除并原子递补（若该生在源场已入选）
    original_enqueued = src.signups[sid].enqueued_at
    events.extend(_cancel_signup_events(state, src, sid, at, reason="transferred_out"))
    # 2) 目标场次报名，沿用原报名时刻，保证跨场等待时长不打折
    target_payload: dict = {
        "student_id": sid,
        "enqueued_at": original_enqueued.isoformat(),
        "via_transfer": True,
    }
    dst.signups[sid] = SignupInfo(student_id=sid, enqueued_at=original_enqueued, via_transfer=True)
    if dst.status == "published":
        promotions, notifications = _fill_vacancies(state, dst, at)
        target_payload["promotions"] = promotions
        target_payload["notifications"] = notifications
    events.append((SIGNED_UP, dst.session_id, target_payload))
    events.append((TRANSFER_ACCEPTED, src.session_id, {"offer_id": offer.offer_id, "to_session": dst.session_id}))
    return events


def _h_decline_transfer(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session, offer = _find_offer(state, p["offer_id"])
    if offer.status != "pending":
        raise ConflictError("offer_decided", "转移申请已处理", 409)
    if p.get("student_id") and p["student_id"] != offer.student_id:
        raise ForbiddenError("not_offer_owner", "学生只能处理本人的转移申请", 403)
    return [(TRANSFER_DECLINED, session.session_id, {"offer_id": offer.offer_id})]


def _h_record_notifications(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    receipts = p["receipts"]
    if not receipts:
        raise ValidationError("empty_receipts", "回执不能为空")
    normalized = []
    for r in receipts:
        nid = r["notification_id"]
        if nid not in session.notifications:
            raise NotFoundError("notification_not_found", f"通知不存在：{nid}", 404)
        normalized.append({"notification_id": nid, "delivered": bool(r["delivered"])})
    return [(NOTIFICATION_RECORDED, p["session_id"], {"receipts": normalized})]


def _h_close_session(state: State, p: dict, at: datetime) -> list[ProducedEvent]:
    session = state.require_session(p["session_id"])
    if session.status == "closed":
        raise ConflictError("session_closed", "场次已关闭", 409)
    return [(SESSION_CLOSED, p["session_id"], {})]


_HANDLERS = {
    "register_class": _h_register_class,
    "schedule_session": _h_schedule_session,
    "register_student": _h_register_student,
    "approve_accommodation": _h_approve_accommodation,
    "grant_consent": _h_grant_consent,
    "withdraw_consent": _h_withdraw_consent,
    "sign_up": _h_sign_up,
    "cancel_signup": _h_cancel_signup,
    "publish_roster": _h_publish_roster,
    "expand_capacity": _h_expand_capacity,
    "mark_attendance": _h_mark_attendance,
    "offer_transfer": _h_offer_transfer,
    "accept_transfer": _h_accept_transfer,
    "decline_transfer": _h_decline_transfer,
    "record_notifications": _h_record_notifications,
    "close_session": _h_close_session,
}
