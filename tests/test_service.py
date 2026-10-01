"""应用层端到端测试：公平轮转、原子递补、幂等、隐私裁剪、转移等。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation_queue.errors import ConflictError, NotFoundError, ValidationError
from rotation_queue.service import QueueService
from rotation_queue.store import EventStore


def bootstrap(nsessions: int = 1, capacity: int = 3) -> tuple[QueueService, EventStore]:
    store = EventStore(":memory:")
    svc = QueueService(store)
    svc.register_class({"class_id": "c1", "name": "三年级1班"})
    for i in range(1, nsessions + 1):
        svc.schedule_session({
            "session_id": f"s{i}",
            "class_id": "c1",
            "starts_at": f"2026-10-0{i}T10:00:00+00:00",
            "capacity": capacity,
        })
    for sid in "abcde":
        svc.register_student({"student_id": sid, "class_id": "c1", "name": f"学生{sid.upper()}"})
        svc.grant_consent({"student_id": sid, "guardian": "监护人X"})
    return svc, store


class FairRotationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store = bootstrap(capacity=2)

    def test_history_count_drives_turn_taking(self) -> None:
        # 上轮参加过的人历史次数高，本轮靠后
        self.svc.sign_up({"session_id": "s1", "student_id": "a",
                          "enqueued_at": "2026-09-20T09:00:00+00:00"})
        self.svc.sign_up({"session_id": "s1", "student_id": "b",
                          "enqueued_at": "2026-09-20T09:01:00+00:00"})
        self.svc.publish_roster({"session_id": "s1"})
        self.svc.mark_attendance({"session_id": "s1", "student_id": "a"})
        self.svc.mark_attendance({"session_id": "s1", "student_id": "b"})
        self.svc.close_session({"session_id": "s1"})

        self.svc.schedule_session({"session_id": "s2", "class_id": "c1",
                                   "starts_at": "2026-10-08T10:00:00+00:00", "capacity": 2})
        # c 报得最晚但历史为 0；a/b 历史为 1
        for sid, ts in [
            ("a", "2026-09-29T08:00:00+00:00"),
            ("b", "2026-09-29T08:01:00+00:00"),
            ("c", "2026-09-29T09:00:00+00:00"),
        ]:
            self.svc.sign_up({"session_id": "s2", "student_id": sid, "enqueued_at": ts})
        pub = self.svc.publish_roster({"session_id": "s2"})
        order = [(r["student_id"], r["selected"]) for r in pub["roster"]]
        # c(0 次) 最前；a、b 同为 1 次，按报名时间 => a,b
        self.assertEqual(order, [("c", True), ("a", True), ("b", False)])

    def test_approved_accommodation_prioritizes_without_leaking_details(self) -> None:
        self.svc.approve_accommodation({
            "student_id": "c", "case_id": "med-1", "reason_type": "medical",
            "evidence_ref": "doc://secret-medical-report.pdf",
        })
        self.svc.sign_up({"session_id": "s1", "student_id": "a",
                          "enqueued_at": "2026-09-20T09:00:00+00:00"})
        self.svc.sign_up({"session_id": "s1", "student_id": "c",
                          "enqueued_at": "2026-09-20T09:30:00+00:00"})
        pub = self.svc.publish_roster({"session_id": "s1"})
        self.assertEqual(pub["roster"][0]["student_id"], "c")

        tv = self.svc.teacher_view("s1")
        row_c = next(r for r in tv["roster"] if r["student_id"] == "c")
        # 教师看得到排序因素与编码标签，但看不到证据编号/医疗细节
        self.assertEqual(row_c["factors"]["accommodation_label"], "医疗类（经批准）")
        self.assertNotIn("evidence_ref", str(tv))
        self.assertNotIn("secret-medical-report", str(tv))
        self.assertNotIn("监护人X", str(tv))


class PromotionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store = bootstrap(capacity=2)
        for i, sid in enumerate("abcd"):
            self.svc.sign_up({"session_id": "s1", "student_id": sid,
                              "enqueued_at": f"2026-09-20T09:0{i}:00+00:00"})
        self.svc.publish_roster({"session_id": "s1"})

    def test_cancel_triggers_atomic_promotion(self) -> None:
        state0 = self.store.load_state()
        selected_before = list(state0.sessions["s1"].selected)
        self.assertEqual(selected_before, ["a", "b"])

        self.svc.cancel_signup({"session_id": "s1", "student_id": "a"})
        tv = self.svc.teacher_view("s1")
        self.assertEqual([r["student_id"] for r in tv["roster"] if r["selected"]], ["b", "c"])

        # 取消事件与递补在同一事件（原子）；promotion 记录在该事件负载内
        events = self.store.list_events("s1")
        cancel_evt = next(e for e in events if e.event_type == "signup_cancelled")
        promoted_ids = [p["student_id"] for p in cancel_evt.payload["promotions"]]
        self.assertEqual(promoted_ids, ["c"])
        # 原子性：事件表里取消与递补不是两个可分离的写操作（同一事件）
        self.assertIn("notifications", cancel_evt.payload)

    def test_consent_withdraw_removes_and_promotes_in_one_command(self) -> None:
        result = self.svc.withdraw_consent({"student_id": "b", "reason": "家长要求"})
        self.assertIn("s1", result["removed_or_promoted_in_sessions"])
        tv = self.svc.teacher_view("s1")
        self.assertEqual([r["student_id"] for r in tv["roster"] if r["selected"]], ["a", "c"])
        # 撤回后不能再报名
        with self.assertRaises(ValidationError) as ctx:
            self.svc.sign_up({"session_id": "s1", "student_id": "b"})
        self.assertEqual(ctx.exception.code, "no_consent")

    def test_expand_capacity_promotes_in_bulk(self) -> None:
        res = self.svc.expand_capacity({"session_id": "s1", "new_capacity": 4})
        promoted = [p["student_id"] for p in res["promoted"]]
        self.assertEqual(promoted, ["c", "d"])
        tv = self.svc.teacher_view("s1")
        self.assertEqual(len([r for r in tv["roster"] if r["selected"]]), 4)
        with self.assertRaises(ValidationError):
            self.svc.expand_capacity({"session_id": "s1", "new_capacity": 3})


class NotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store = bootstrap(capacity=2)
        for i, sid in enumerate("abc"):
            self.svc.sign_up({"session_id": "s1", "student_id": sid,
                              "enqueued_at": f"2026-09-20T09:0{i}:00+00:00"})

    def test_delivery_receipts_recorded(self) -> None:
        self.svc.publish_roster({"session_id": "s1"})
        # 教师视图暴露通知 ID，凭此即可上报回执，无需触碰内部存储
        tv = self.svc.teacher_view("s1")
        rows = {r["student_id"]: r for r in tv["roster"]}
        self.assertEqual({r["notification"]["kind"] for r in tv["roster"]},
                         {"selected", "waitlisted"})
        for r in tv["roster"]:
            self.assertIsNone(r["notification"]["delivered"])
            self.assertTrue(r["notifications"])  # 每人至少一条

        waitlist_id = rows["c"]["notification"]["notification_id"]
        self.svc.record_notifications({
            "session_id": "s1",
            "receipts": [{"notification_id": waitlist_id, "delivered": False}],
        })
        ov = self.svc.overview("s1")
        self.assertEqual(ov["notifications_undelivered"], 1)
        self.assertEqual(ov["notifications_pending"], 2)

        # 回执可更正（重报 True 覆盖 False）
        self.svc.record_notifications({
            "session_id": "s1",
            "receipts": [{"notification_id": waitlist_id, "delivered": True}],
        })
        tv2 = self.svc.teacher_view("s1")
        c_row = next(r for r in tv2["roster"] if r["student_id"] == "c")
        self.assertTrue(c_row["notification"]["delivered"])

        # 未知通知号报错
        with self.assertRaises(NotFoundError):
            self.svc.record_notifications({
                "session_id": "s1",
                "receipts": [{"notification_id": "notif:bogus", "delivered": True}],
            })


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store = bootstrap(capacity=3)

    def test_replay_same_key_does_not_double_occupy(self) -> None:
        payload = {"session_id": "s1", "student_id": "a",
                   "enqueued_at": "2026-09-20T09:00:00+00:00"}
        r1 = self.svc.sign_up(payload, idem_key="signup-a-s1")
        r2 = self.svc.sign_up(payload, idem_key="signup-a-s1")
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        state = self.store.load_state()
        signup_events = [e for e in self.store.list_events() if e.event_type == "signed_up"
                         and e.payload["student_id"] == "a"]
        self.assertEqual(len(signup_events), 1)

    def test_different_keys_same_business_key_still_conflicts(self) -> None:
        payload = {"session_id": "s1", "student_id": "a"}
        self.svc.sign_up(payload, idem_key="key-1")
        with self.assertRaises(ConflictError) as ctx:
            self.svc.sign_up(payload, idem_key="key-2")
        self.assertEqual(ctx.exception.code, "already_signed_up")


class TransferTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store = bootstrap(nsessions=2, capacity=2)
        for i, sid in enumerate("abcd"):
            self.svc.sign_up({"session_id": "s1", "student_id": sid,
                              "enqueued_at": f"2026-09-20T09:0{i}:00+00:00"})
        self.svc.publish_roster({"session_id": "s1"})  # a,b 入选；c,d 候补

    def test_accept_transfer_removes_promotes_and_keeps_enqueue_time(self) -> None:
        # b 已入选 s1，请求转到 s2；接受后 s1 原子递补 c，b 在 s2 沿用 09:01 的报名时刻
        self.svc.offer_transfer({
            "offer_id": "o1", "from_session": "s1", "to_session": "s2", "student_id": "b",
        })
        # 同一生意重复发起待处理申请 -> 冲突
        with self.assertRaises(ConflictError):
            self.svc.offer_transfer({
                "offer_id": "o2", "from_session": "s1", "to_session": "s2", "student_id": "b",
            })
        self.svc.accept_transfer({"offer_id": "o1"})
        tv1 = self.svc.teacher_view("s1")
        self.assertEqual([r["student_id"] for r in tv1["roster"] if r["selected"]], ["a", "c"])
        preview = self.svc.preview_roster("s2")
        b_row = next(r for r in preview["roster"] if r["student_id"] == "b")
        self.assertTrue(b_row["factors"]["transferred_in"])
        self.assertIn("沿用原报名时间", b_row["reason_text"])
        # 重复接受已决定的申请 -> 冲突
        with self.assertRaises(ConflictError):
            self.svc.accept_transfer({"offer_id": "o1"})

    def test_decline_leaves_everything_in_place(self) -> None:
        self.svc.offer_transfer({
            "offer_id": "o1", "from_session": "s1", "to_session": "s2", "student_id": "c",
        })
        self.svc.decline_transfer({"offer_id": "o1"})
        tv1 = self.svc.teacher_view("s1")
        self.assertEqual([r["student_id"] for r in tv1["roster"] if r["selected"]], ["a", "b"])

    def test_cannot_transfer_between_classes(self) -> None:
        self.svc.register_class({"class_id": "c2", "name": "另一班"})
        self.svc.schedule_session({"session_id": "s3", "class_id": "c2",
                                   "starts_at": "2026-10-05T10:00:00+00:00", "capacity": 2})
        with self.assertRaises(ValidationError) as ctx:
            self.svc.offer_transfer({
                "offer_id": "o9", "from_session": "s1", "to_session": "s3", "student_id": "a",
            })
        self.assertEqual(ctx.exception.code, "class_mismatch")


class AttendanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, self.store = bootstrap(capacity=2)
        for i, sid in enumerate("abc"):
            self.svc.sign_up({"session_id": "s1", "student_id": sid,
                              "enqueued_at": f"2026-09-20T09:0{i}:00+00:00"})
        self.svc.publish_roster({"session_id": "s1"})

    def test_simultaneous_duplicate_checkin_rejected(self) -> None:
        self.svc.mark_attendance({"session_id": "s1", "student_id": "a"})
        with self.assertRaises(ConflictError) as ctx:
            self.svc.mark_attendance({"session_id": "s1", "student_id": "a"})
        self.assertEqual(ctx.exception.code, "already_checked_in")
        state = self.store.load_state()
        self.assertEqual(state.sessions["s1"].attended, {"a"})

    def test_waitlisted_cannot_checkin(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            self.svc.mark_attendance({"session_id": "s1", "student_id": "c"})
        self.assertEqual(ctx.exception.code, "not_on_roster")


if __name__ == "__main__":
    unittest.main()
