"""学生互动轮转排队核心服务。

设计要点：
- 一切变更都是携带 event_id 的命令；同一 event_id 重放返回首次记录的结果，
  绝不重复占位（events 表唯一约束 + entries 部分唯一索引双保险）；
- 每个命令在单个 BEGIN IMMEDIATE 事务内完成状态变更与原子递补，
  处理器内部出错时回滚到 SAVEPOINT，只把错误结果写进事件日志；
- 递补通知与状态变更同事务落库（pending），提交后由网关派发并回写
  sent/failed，送达确认由 record_notification_result 命令记录 delivered/failed。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import (
    AlreadyRegistered,
    CapacityBelowOccupied,
    ConsentRequired,
    DomainError,
    EntryNotActive,
    EntryNotTransferable,
    GrantStateError,
    NotEligible,
    NotFound,
    NotOffered,
    NotificationStateError,
    SessionClosed,
    ValidationError,
)
from .policy import Factors, explain, sort_key, tie_group_sizes, tie_identity
from .storage import Database

ACTIVE_ENTRY_STATUSES = ("queued", "offered", "checked_in")


def utc_now_iso() -> str:
    """默认时钟：UTC ISO 微秒串，字典序即时间序。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_ts(value: Any, fallback: str | None) -> str | None:
    """把输入时间规范为 UTC ISO 微秒串，保证全库时间可按字典序比较。"""
    if value is None:
        return fallback
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"时间格式无效：{value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _require(payload: dict, *keys: str) -> None:
    missing = [k for k in keys if payload.get(k) is None or payload.get(k) == ""]
    if missing:
        raise ValidationError("缺少字段：" + "、".join(missing))


def _new_id() -> str:
    return uuid.uuid4().hex


class RotationService:
    """轮转排队命令服务。所有公开变更都经过 execute 以获得幂等保证。"""

    def __init__(
        self,
        db: str | Database,
        *,
        gateway: Any = None,
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self.db = db if isinstance(db, Database) else Database(db)
        self.gateway = gateway  # 通知网关：send(notification: dict) -> bool
        self.clock = clock

    # ------------------------------------------------------------------
    # 命令入口（幂等）
    # ------------------------------------------------------------------

    def execute(
        self,
        kind: str,
        payload: dict,
        *,
        event_id: str | None = None,
        actor: str | None = None,
    ) -> dict:
        """执行一条命令。同一 event_id 重放返回首次结果，不产生副作用。"""
        if not isinstance(payload, dict):
            return self._failure("validation_error", "payload 必须是对象")
        handler = getattr(self, f"_cmd_{kind}", None)
        if handler is None:
            return self._failure("unknown_command", f"未知命令：{kind}")
        event_id = event_id or _new_id()
        outbox: list[str] = []
        with self.db.tx() as conn:
            prior = conn.execute(
                "SELECT result FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if prior is not None:
                result = json.loads(prior["result"])
                result["replayed"] = True
                return result
            now = self.clock()
            conn.execute("SAVEPOINT cmd")
            try:
                body = handler(conn, now, outbox, payload)
            except DomainError as exc:
                conn.execute("ROLLBACK TO cmd")
                conn.execute("RELEASE cmd")
                outbox.clear()  # 通知行已随回滚消失，不能进入结果与派发
                result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            else:
                conn.execute("RELEASE cmd")
                result = {"ok": True, **body}
            if outbox:
                result["notifications"] = [
                    {"notification_id": nid, "status": "pending"} for nid in outbox
                ]
            conn.execute(
                "INSERT INTO events (event_id, kind, actor, payload, result, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    kind,
                    actor,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
        # 提交后再派发通知；派发结果回写事件日志，保证重放看到一致快照
        if outbox and self.gateway is not None:
            final = self._dispatch(outbox)
            for item in result.get("notifications", []):
                item["status"] = final.get(item["notification_id"], item["status"])
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE events SET result = ? WHERE event_id = ?",
                    (json.dumps(result, ensure_ascii=False, sort_keys=True), event_id),
                )
        result["replayed"] = False
        return result

    @staticmethod
    def _failure(code: str, message: str) -> dict:
        return {"ok": False, "error": {"code": code, "message": message}, "replayed": False}

    # ------------------------------------------------------------------
    # 通知派发
    # ------------------------------------------------------------------

    def _dispatch(self, notification_ids: list[str]) -> dict[str, str]:
        """把 pending 通知交给网关，按结果落库 sent/failed。"""
        final: dict[str, str] = {}
        with self.db.tx() as conn:
            for nid in notification_ids:
                row = conn.execute(
                    "SELECT * FROM notifications WHERE notification_id = ?", (nid,)
                ).fetchone()
                if row is None:
                    continue
                if row["status"] != "pending":
                    final[nid] = row["status"]
                    continue
                try:
                    ok = bool(self.gateway.send(dict(row)))
                except Exception:
                    ok = False
                status = "sent" if ok else "failed"
                conn.execute(
                    "UPDATE notifications SET status = ?, attempts = attempts + 1,"
                    " updated_at = ? WHERE notification_id = ? AND status = 'pending'",
                    (status, self.clock(), nid),
                )
                final[nid] = status
        return final

    def dispatch_pending(self, limit: int = 100) -> dict[str, str]:
        """补发遗留的 pending 通知（例如进程在提交后、派发前崩溃）。"""
        if self.gateway is None:
            return {}
        with self.db.read() as conn:
            ids = [
                row["notification_id"]
                for row in conn.execute(
                    "SELECT notification_id FROM notifications WHERE status = 'pending'"
                    " ORDER BY created_at, notification_id LIMIT ?",
                    (limit,),
                )
            ]
        return self._dispatch(ids) if ids else {}

    # ------------------------------------------------------------------
    # 领域查询助手
    # ------------------------------------------------------------------

    def _get_session(self, conn, session_id: str):
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"场次不存在：{session_id}")
        return row

    def _get_student(self, conn, student_id: str):
        row = conn.execute(
            "SELECT * FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"学生不存在：{student_id}")
        return row

    @staticmethod
    def _require_open(session) -> None:
        if session["state"] != "open":
            raise SessionClosed(f"场次 {session['session_id']} 已结束")

    @staticmethod
    def _eligible_classes(conn, session_id: str) -> set[str]:
        return {
            row["class_id"]
            for row in conn.execute(
                "SELECT class_id FROM session_eligible_classes WHERE session_id = ?",
                (session_id,),
            )
        }

    @staticmethod
    def _require_consent(conn, student_id: str) -> None:
        row = conn.execute(
            "SELECT status FROM consents WHERE student_id = ?", (student_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            raise ConsentRequired(f"学生 {student_id} 缺少有效监护授权")

    @staticmethod
    def _active_entry(conn, session_id: str, student_id: str):
        return conn.execute(
            "SELECT * FROM entries WHERE session_id = ? AND student_id = ?"
            " AND status IN ('queued', 'offered', 'checked_in')",
            (session_id, student_id),
        ).fetchone()

    @staticmethod
    def _care_tier(conn, student_id: str, now: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(tier), 0) AS tier FROM care_grants"
            " WHERE student_id = ? AND status = 'approved'"
            " AND (valid_until IS NULL OR valid_until > ?)",
            (student_id, now),
        ).fetchone()
        return row["tier"]

    @staticmethod
    def _participation_count(conn, student_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM entries WHERE student_id = ? AND status = 'completed'",
            (student_id,),
        ).fetchone()
        return row["c"]

    def _factors(self, conn, entry_row, now: str) -> Factors:
        return Factors(
            care_tier=self._care_tier(conn, entry_row["student_id"], now),
            participation_count=self._participation_count(conn, entry_row["student_id"]),
            registered_at=entry_row["registered_at"],
            student_id=entry_row["student_id"],
        )

    # ------------------------------------------------------------------
    # 原子递补：在调用方事务内运行，空位、递补、通知同事务生效
    # ------------------------------------------------------------------

    def _rebalance(self, conn, session_id: str, now: str, outbox: list[str]) -> list[dict]:
        session = conn.execute(
            "SELECT capacity, state FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None or session["state"] != "open":
            return []
        occupied = conn.execute(
            "SELECT COUNT(*) AS c FROM entries WHERE session_id = ?"
            " AND status IN ('offered', 'checked_in')",
            (session_id,),
        ).fetchone()["c"]
        free = session["capacity"] - occupied
        if free <= 0:
            return []
        rows = conn.execute(
            "SELECT e.* FROM entries e"
            " JOIN consents c ON c.student_id = e.student_id AND c.status = 'active'"
            " WHERE e.session_id = ? AND e.status = 'queued'",
            (session_id,),
        ).fetchall()
        if not rows:
            return []
        factored = [(self._factors(conn, row, now), row) for row in rows]
        factored.sort(key=lambda item: sort_key(item[0]))
        ties = tie_group_sizes(f for f, _ in factored)
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(offer_seq), 0) + 1 AS n FROM entries WHERE session_id = ?",
            (session_id,),
        ).fetchone()["n"]
        promoted = []
        for factors, row in factored[:free]:
            rationale = explain(factors, tie_group_size=ties[tie_identity(factors)])
            rationale["capacity_after"] = session["capacity"]
            cursor = conn.execute(
                "UPDATE entries SET status = 'offered', offered_at = ?, offer_seq = ?,"
                " offer_explanation = ?, updated_at = ?"
                " WHERE entry_id = ? AND status = 'queued'",
                (
                    now,
                    next_seq,
                    json.dumps(rationale, ensure_ascii=False, sort_keys=True),
                    now,
                    row["entry_id"],
                ),
            )
            if cursor.rowcount != 1:
                continue  # 状态已被同事务内前序操作改变，以数据库为准
            next_seq += 1
            notification_id = _new_id()
            conn.execute(
                "INSERT INTO notifications (notification_id, entry_id, kind, status,"
                " created_at, updated_at) VALUES (?, ?, 'offer', 'pending', ?, ?)",
                (notification_id, row["entry_id"], now, now),
            )
            outbox.append(notification_id)
            promoted.append(
                {
                    "entry_id": row["entry_id"],
                    "student_id": row["student_id"],
                    "notification_id": notification_id,
                    "rationale": rationale,
                }
            )
        return promoted

    # ------------------------------------------------------------------
    # 命令处理器：签名 (conn, now, outbox, payload) -> dict
    # ------------------------------------------------------------------

    def _cmd_create_student(self, conn, now, outbox, p):
        _require(p, "student_id", "class_id", "name")
        try:
            conn.execute(
                "INSERT INTO students (student_id, class_id, name, guardian_contact,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (p["student_id"], p["class_id"], p["name"], p.get("guardian_contact"), now),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(f"学生已存在：{p['student_id']}", code="student_exists")
        return {"student_id": p["student_id"], "class_id": p["class_id"]}

    def _cmd_create_session(self, conn, now, outbox, p):
        _require(p, "session_id", "class_id", "title", "capacity", "starts_at")
        capacity = p["capacity"]
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
            raise ValidationError("capacity 必须是非负整数")
        starts_at = normalize_ts(p["starts_at"], None)
        try:
            conn.execute(
                "INSERT INTO sessions (session_id, class_id, title, capacity, starts_at,"
                " state, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?)",
                (p["session_id"], p["class_id"], p["title"], capacity, starts_at, now),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(f"场次已存在：{p['session_id']}", code="session_exists")
        classes = {p["class_id"], *p.get("eligible_class_ids", [])}
        for class_id in sorted(classes):
            conn.execute(
                "INSERT INTO session_eligible_classes (session_id, class_id) VALUES (?, ?)",
                (p["session_id"], class_id),
            )
        return {
            "session_id": p["session_id"],
            "capacity": capacity,
            "eligible_classes": sorted(classes),
        }

    def _cmd_grant_consent(self, conn, now, outbox, p):
        _require(p, "student_id")
        self._get_student(conn, p["student_id"])
        conn.execute(
            "INSERT INTO consents (student_id, status, updated_at) VALUES (?, 'active', ?)"
            " ON CONFLICT(student_id) DO UPDATE SET status = 'active',"
            " updated_at = excluded.updated_at",
            (p["student_id"], now),
        )
        return {"student_id": p["student_id"], "consent": "active"}

    def _cmd_withdraw_consent(self, conn, now, outbox, p):
        """监护授权撤回：排队/占位记录确定性移除并递补；已签到记录保留并标记。"""
        _require(p, "student_id")
        self._get_student(conn, p["student_id"])
        conn.execute(
            "INSERT INTO consents (student_id, status, updated_at) VALUES (?, 'withdrawn', ?)"
            " ON CONFLICT(student_id) DO UPDATE SET status = 'withdrawn',"
            " updated_at = excluded.updated_at",
            (p["student_id"], now),
        )
        rows = conn.execute(
            "SELECT * FROM entries WHERE student_id = ?"
            " AND status IN ('queued', 'offered', 'checked_in')"
            " ORDER BY session_id, entry_id",
            (p["student_id"],),
        ).fetchall()
        removed, flagged, promotions = [], [], {}
        for row in rows:
            if row["status"] == "checked_in":
                # 人已到场：不抹掉签到事实，标记给工作人员跟进
                flagged.append({"session_id": row["session_id"], "entry_id": row["entry_id"]})
                continue
            conn.execute(
                "UPDATE entries SET status = 'removed', exit_reason = 'consent_withdrawn',"
                " updated_at = ? WHERE entry_id = ?",
                (now, row["entry_id"]),
            )
            removed.append({"session_id": row["session_id"], "entry_id": row["entry_id"]})
            if row["status"] == "offered":
                promoted = self._rebalance(conn, row["session_id"], now, outbox)
                if promoted:
                    promotions[row["session_id"]] = promoted
        return {
            "student_id": p["student_id"],
            "consent": "withdrawn",
            "removed": removed,
            "flagged_checked_in": flagged,
            "promotions": promotions,
        }

    def _cmd_submit_care_grant(self, conn, now, outbox, p):
        _require(p, "grant_id", "student_id", "tier", "detail")
        self._get_student(conn, p["student_id"])
        tier = p["tier"]
        if not isinstance(tier, int) or isinstance(tier, bool) or not 1 <= tier <= 9:
            raise ValidationError("tier 必须是 1-9 的整数")
        valid_until = normalize_ts(p.get("valid_until"), None)
        try:
            conn.execute(
                "INSERT INTO care_grants (grant_id, student_id, tier, detail, status,"
                " valid_until, submitted_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (p["grant_id"], p["student_id"], tier, p["detail"], valid_until, now),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(f"照顾依据已存在：{p['grant_id']}", code="grant_exists")
        return {"grant_id": p["grant_id"], "status": "pending"}

    def _get_grant(self, conn, grant_id: str):
        row = conn.execute(
            "SELECT * FROM care_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"照顾依据不存在：{grant_id}")
        return row

    def _cmd_approve_care_grant(self, conn, now, outbox, p):
        _require(p, "grant_id")
        grant = self._get_grant(conn, p["grant_id"])
        if grant["status"] != "pending":
            raise GrantStateError(f"当前状态 {grant['status']} 不能批准")
        conn.execute(
            "UPDATE care_grants SET status = 'approved', decided_at = ?, decided_by = ?"
            " WHERE grant_id = ?",
            (now, p.get("decided_by"), p["grant_id"]),
        )
        return {"grant_id": p["grant_id"], "status": "approved"}

    def _cmd_revoke_care_grant(self, conn, now, outbox, p):
        _require(p, "grant_id")
        grant = self._get_grant(conn, p["grant_id"])
        if grant["status"] == "revoked":
            raise GrantStateError("照顾依据已撤回")
        conn.execute(
            "UPDATE care_grants SET status = 'revoked', decided_at = ?, decided_by = ?"
            " WHERE grant_id = ?",
            (now, p.get("decided_by"), p["grant_id"]),
        )
        return {"grant_id": p["grant_id"], "status": "revoked"}

    def _cmd_register(self, conn, now, outbox, p):
        _require(p, "session_id", "student_id")
        session = self._get_session(conn, p["session_id"])
        self._require_open(session)
        student = self._get_student(conn, p["student_id"])
        eligible = self._eligible_classes(conn, session["session_id"])
        if student["class_id"] not in eligible:
            raise NotEligible(f"班级 {student['class_id']} 不在本场次接收范围")
        self._require_consent(conn, student["student_id"])
        registered_at = normalize_ts(p.get("registered_at"), now)
        entry_id = _new_id()
        try:
            conn.execute(
                "INSERT INTO entries (entry_id, session_id, student_id, status,"
                " registered_at, updated_at) VALUES (?, ?, ?, 'queued', ?, ?)",
                (entry_id, session["session_id"], student["student_id"], registered_at, now),
            )
        except sqlite3.IntegrityError:
            raise AlreadyRegistered(
                f"学生 {student['student_id']} 已在场次 {session['session_id']} 的队列中"
            )
        promoted = self._rebalance(conn, session["session_id"], now, outbox)
        status = conn.execute(
            "SELECT status FROM entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()["status"]
        return {
            "entry_id": entry_id,
            "session_id": session["session_id"],
            "student_id": student["student_id"],
            "status": status,
            "promoted": promoted,
        }

    def _cmd_withdraw_entry(self, conn, now, outbox, p):
        """临时退出：释放名额并在同一事务内原子递补。"""
        _require(p, "session_id", "student_id")
        self._get_session(conn, p["session_id"])
        entry = self._active_entry(conn, p["session_id"], p["student_id"])
        if entry is None or entry["status"] == "checked_in":
            raise EntryNotActive("没有可退出的有效报名（已签到记录请走场次结算）")
        conn.execute(
            "UPDATE entries SET status = 'withdrawn', exit_reason = ?, updated_at = ?"
            " WHERE entry_id = ?",
            (p.get("reason") or "withdrawn", now, entry["entry_id"]),
        )
        promoted = []
        if entry["status"] == "offered":
            promoted = self._rebalance(conn, p["session_id"], now, outbox)
        return {
            "entry_id": entry["entry_id"],
            "freed_slot": entry["status"] == "offered",
            "promoted": promoted,
        }

    def _cmd_transfer(self, conn, now, outbox, p):
        """跨场转移：源场次退出（空位即时递补）与目标场次入队同事务完成，
        保留原始报名时间，保证转移不会重置排队资历。"""
        _require(p, "student_id", "from_session_id", "to_session_id")
        if p["from_session_id"] == p["to_session_id"]:
            raise ValidationError("源场次与目标场次相同")
        source = self._active_entry(conn, p["from_session_id"], p["student_id"])
        if source is None:
            raise EntryNotActive("源场次中没有该学生的有效报名")
        if source["status"] == "checked_in":
            raise EntryNotTransferable("已签到的记录不能跨场转移")
        target = self._get_session(conn, p["to_session_id"])
        self._require_open(target)
        student = self._get_student(conn, p["student_id"])
        eligible = self._eligible_classes(conn, target["session_id"])
        if student["class_id"] not in eligible:
            raise NotEligible(f"班级 {student['class_id']} 不在目标场次接收范围")
        self._require_consent(conn, p["student_id"])
        conn.execute(
            "UPDATE entries SET status = 'withdrawn', exit_reason = 'transferred_out',"
            " updated_at = ? WHERE entry_id = ?",
            (now, source["entry_id"]),
        )
        new_entry_id = _new_id()
        try:
            conn.execute(
                "INSERT INTO entries (entry_id, session_id, student_id, status,"
                " registered_at, transferred_from, updated_at)"
                " VALUES (?, ?, ?, 'queued', ?, ?, ?)",
                (
                    new_entry_id,
                    target["session_id"],
                    p["student_id"],
                    source["registered_at"],
                    p["from_session_id"],
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise AlreadyRegistered(
                f"学生 {p['student_id']} 已在目标场次 {p['to_session_id']} 的队列中"
            )
        promoted_from = (
            self._rebalance(conn, p["from_session_id"], now, outbox)
            if source["status"] == "offered"
            else []
        )
        promoted_to = self._rebalance(conn, p["to_session_id"], now, outbox)
        new_status = conn.execute(
            "SELECT status FROM entries WHERE entry_id = ?", (new_entry_id,)
        ).fetchone()["status"]
        return {
            "student_id": p["student_id"],
            "from": {
                "session_id": p["from_session_id"],
                "entry_id": source["entry_id"],
                "promoted": promoted_from,
            },
            "to": {
                "session_id": p["to_session_id"],
                "entry_id": new_entry_id,
                "status": new_status,
                "registered_at": source["registered_at"],
                "promoted": promoted_to,
            },
        }

    def _cmd_set_capacity(self, conn, now, outbox, p):
        """临时扩容/缩容：扩容立即按序补足空位；缩容不得低于已占席位。"""
        _require(p, "session_id", "capacity")
        session = self._get_session(conn, p["session_id"])
        self._require_open(session)
        capacity = p["capacity"]
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
            raise ValidationError("capacity 必须是非负整数")
        occupied = conn.execute(
            "SELECT COUNT(*) AS c FROM entries WHERE session_id = ?"
            " AND status IN ('offered', 'checked_in')",
            (p["session_id"],),
        ).fetchone()["c"]
        if capacity < occupied:
            raise CapacityBelowOccupied(f"当前已占 {occupied} 席，容量不能缩至 {capacity}")
        conn.execute(
            "UPDATE sessions SET capacity = ? WHERE session_id = ?",
            (capacity, p["session_id"]),
        )
        promoted = self._rebalance(conn, p["session_id"], now, outbox)
        return {
            "session_id": p["session_id"],
            "capacity": capacity,
            "occupied": occupied,
            "promoted": promoted,
        }

    def _cmd_check_in(self, conn, now, outbox, p):
        """签到：仅已获名额者可签到；重复签到是确定性空操作。"""
        _require(p, "session_id", "student_id")
        self._get_session(conn, p["session_id"])
        entry = self._active_entry(conn, p["session_id"], p["student_id"])
        if entry is None:
            raise EntryNotActive("没有有效报名记录")
        if entry["status"] == "checked_in":
            return {
                "entry_id": entry["entry_id"],
                "status": "checked_in",
                "already_checked_in": True,
                "checked_in_at": entry["checked_in_at"],
            }
        if entry["status"] != "offered":
            raise NotOffered("未获得名额，不能签到")
        checked_at = normalize_ts(p.get("checked_at"), now)
        conn.execute(
            "UPDATE entries SET status = 'checked_in', checked_in_at = ?, updated_at = ?"
            " WHERE entry_id = ?",
            (checked_at, now, entry["entry_id"]),
        )
        return {
            "entry_id": entry["entry_id"],
            "status": "checked_in",
            "already_checked_in": False,
            "checked_in_at": checked_at,
        }

    def _cmd_complete_session(self, conn, now, outbox, p):
        """场次结算：签到者计入历史参与，占位未签到记缺席，排队者释放。"""
        _require(p, "session_id")
        session = self._get_session(conn, p["session_id"])
        self._require_open(session)
        completed = conn.execute(
            "UPDATE entries SET status = 'completed', updated_at = ?"
            " WHERE session_id = ? AND status = 'checked_in'",
            (now, p["session_id"]),
        ).rowcount
        no_show = conn.execute(
            "UPDATE entries SET status = 'no_show', updated_at = ?"
            " WHERE session_id = ? AND status = 'offered'",
            (now, p["session_id"]),
        ).rowcount
        released = conn.execute(
            "UPDATE entries SET status = 'removed', exit_reason = 'session_closed',"
            " updated_at = ? WHERE session_id = ? AND status = 'queued'",
            (now, p["session_id"]),
        ).rowcount
        conn.execute(
            "UPDATE sessions SET state = 'closed' WHERE session_id = ?", (p["session_id"],)
        )
        return {
            "session_id": p["session_id"],
            "completed": completed,
            "no_show": no_show,
            "released": released,
        }

    def _cmd_record_notification_result(self, conn, now, outbox, p):
        """登记通知送达结果：仅 sent 状态可确认 delivered/failed。"""
        _require(p, "notification_id", "outcome")
        if p["outcome"] not in ("delivered", "failed"):
            raise ValidationError("outcome 必须是 delivered 或 failed")
        row = conn.execute(
            "SELECT * FROM notifications WHERE notification_id = ?",
            (p["notification_id"],),
        ).fetchone()
        if row is None:
            raise NotFound(f"通知不存在：{p['notification_id']}")
        if row["status"] != "sent":
            raise NotificationStateError(
                f"通知当前状态 {row['status']} 不能登记送达结果"
            )
        conn.execute(
            "UPDATE notifications SET status = ?, detail = ?, updated_at = ?"
            " WHERE notification_id = ?",
            (p["outcome"], p.get("detail"), now, p["notification_id"]),
        )
        return {"notification_id": p["notification_id"], "status": p["outcome"]}

    # ------------------------------------------------------------------
    # 队列快照（视图层的数据源，含排序理由，不含敏感字段）
    # ------------------------------------------------------------------

    def queue_snapshot(self, session_id: str) -> dict:
        with self.db.read() as conn:
            session = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFound(f"场次不存在：{session_id}")
            offered_rows = conn.execute(
                "SELECT e.*, s.name, s.class_id AS student_class FROM entries e"
                " JOIN students s ON s.student_id = e.student_id"
                " WHERE e.session_id = ? AND e.status IN ('offered', 'checked_in')"
                " ORDER BY e.offer_seq, e.entry_id",
                (session_id,),
            ).fetchall()
            queued_rows = conn.execute(
                "SELECT e.*, s.name, s.class_id AS student_class FROM entries e"
                " JOIN students s ON s.student_id = e.student_id"
                " WHERE e.session_id = ? AND e.status = 'queued'",
                (session_id,),
            ).fetchall()
            now = self.clock()
            factored = [(self._factors(conn, row, now), row) for row in queued_rows]
            factored.sort(key=lambda item: sort_key(item[0]))
            ties = tie_group_sizes(f for f, _ in factored)
            queued = [
                {
                    "rank": rank,
                    "entry_id": row["entry_id"],
                    "student_id": row["student_id"],
                    "name": row["name"],
                    "class_id": row["student_class"],
                    "status": row["status"],
                    "rationale": explain(factors, tie_group_size=ties[tie_identity(factors)]),
                }
                for rank, (factors, row) in enumerate(factored, start=1)
            ]
            offered = []
            for position, row in enumerate(offered_rows, start=1):
                note = conn.execute(
                    "SELECT notification_id, kind, status, attempts FROM notifications"
                    " WHERE entry_id = ? ORDER BY created_at DESC, notification_id DESC"
                    " LIMIT 1",
                    (row["entry_id"],),
                ).fetchone()
                offered.append(
                    {
                        "position": position,
                        "entry_id": row["entry_id"],
                        "student_id": row["student_id"],
                        "name": row["name"],
                        "class_id": row["student_class"],
                        "status": row["status"],
                        "offered_at": row["offered_at"],
                        "checked_in_at": row["checked_in_at"],
                        "offer_rationale": json.loads(row["offer_explanation"])
                        if row["offer_explanation"]
                        else None,
                        "notification": dict(note) if note else None,
                    }
                )
            return {
                "session": {
                    "session_id": session["session_id"],
                    "title": session["title"],
                    "class_id": session["class_id"],
                    "capacity": session["capacity"],
                    "state": session["state"],
                    "starts_at": session["starts_at"],
                },
                "offered": offered,
                "queued": queued,
            }


# 全部命令类型（HTTP 层据此区分“未知命令”与“无权限”），由处理器方法自动派生
COMMANDS = tuple(
    name[len("_cmd_"):] for name in dir(RotationService) if name.startswith("_cmd_")
)
