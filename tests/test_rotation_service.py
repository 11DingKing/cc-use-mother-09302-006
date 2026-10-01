"""轮转排队核心服务的回归测试：公平排序、原子递补、幂等与确定性规则。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation import views
from rotation.service import RotationService

T0 = "2026-09-30T08:00:00+00:00"
T1 = "2026-09-30T08:01:00+00:00"
T2 = "2026-09-30T08:02:00+00:00"


class FakeClock:
    """单调递增的测试时钟。"""

    def __init__(self) -> None:
        self.tick = 0

    def __call__(self) -> str:
        self.tick += 1
        return f"2026-09-30T07:00:00.{self.tick:06d}+00:00"


class FakeGateway:
    """记录派发、可控制失败的通知网关。"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_next = 0

    def send(self, notification: dict) -> bool:
        if self.fail_next > 0:
            self.fail_next -= 1
            return False
        self.sent.append(notification["notification_id"])
        return True


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway = FakeGateway()
        self.service = RotationService(
            str(Path(self.tmp.name) / "test.db"), gateway=self.gateway, clock=FakeClock()
        )

    # -- 测试辅助 ------------------------------------------------------

    def cmd(self, kind, payload, event_id):
        return self.service.execute(kind, payload, event_id=event_id, actor="tester")

    def make_student(self, sid, class_id="C1", contact=None):
        payload = {"student_id": sid, "class_id": class_id, "name": f"学生{sid}"}
        if contact:
            payload["guardian_contact"] = contact
        result = self.cmd("create_student", payload, f"mk-stu-{sid}")
        self.assertTrue(result["ok"], result)
        return result

    def make_consent(self, sid):
        result = self.cmd("grant_consent", {"student_id": sid}, f"consent-{sid}")
        self.assertTrue(result["ok"], result)

    def make_session(self, sid, capacity=1, class_id="C1", eligible=None):
        payload = {
            "session_id": sid,
            "class_id": class_id,
            "title": f"木偶互动课{sid}",
            "capacity": capacity,
            "starts_at": "2026-10-01T08:00:00+00:00",
        }
        if eligible:
            payload["eligible_class_ids"] = eligible
        result = self.cmd("create_session", payload, f"mk-sess-{sid}")
        self.assertTrue(result["ok"], result)

    def register(self, session_id, sid, at=None, event_id=None):
        payload = {"session_id": session_id, "student_id": sid}
        if at:
            payload["registered_at"] = at
        return self.cmd("register", payload, event_id or f"reg-{session_id}-{sid}")

    def enroll(self, sid, class_id="C1"):
        self.make_student(sid, class_id)
        self.make_consent(sid)

    def give_history(self, sid, count):
        """让学生完成 count 次历史参与。"""
        for i in range(count):
            hid = f"hist-{sid}-{i}"
            self.make_session(hid, capacity=1)
            self.assertEqual(self.register(hid, sid)["status"], "offered")
            self.cmd("check_in", {"session_id": hid, "student_id": sid}, f"ci-{hid}")
            done = self.cmd("complete_session", {"session_id": hid}, f"done-{hid}")
            self.assertEqual(done["completed"], 1)

    def entry_count(self, session_id):
        with self.service.db.read() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE session_id = ?"
                " AND status IN ('queued', 'offered', 'checked_in')",
                (session_id,),
            ).fetchone()["c"]

    def statuses(self, session_id):
        snapshot = self.service.queue_snapshot(session_id)
        return {
            "offered": [i["student_id"] for i in snapshot["offered"]],
            "queued": [i["student_id"] for i in snapshot["queued"]],
        }


class OrderingTest(ServiceTestCase):
    def test_history_count_then_registration_time(self) -> None:
        """参与次数少者优先；次数相同报名早者优先。"""
        for sid in ("s_old", "s_new1", "s_new2"):
            self.enroll(sid)
        self.give_history("s_old", 1)
        self.make_session("S", capacity=0)
        self.register("S", "s_old", at=T0)
        self.register("S", "s_new2", at=T2)
        self.register("S", "s_new1", at=T1)
        result = self.cmd("set_capacity", {"session_id": "S", "capacity": 2}, "cap-S")
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["s_new1", "s_new2"])
        state = self.statuses("S")
        self.assertEqual(state["offered"], ["s_new1", "s_new2"])
        self.assertEqual(state["queued"], ["s_old"])
        snapshot = self.service.queue_snapshot("S")
        self.assertEqual(snapshot["queued"][0]["rationale"]["participation_count"], 1)

    def test_approved_care_grant_outranks_earlier_registration(self) -> None:
        """只有经批准且在有效期内的照顾依据才参与排序。"""
        self.enroll("s_care")
        self.enroll("s_plain")
        self.cmd(
            "submit_care_grant",
            {"grant_id": "g1", "student_id": "s_care", "tier": 2, "detail": "敏感细节"},
            "g1",
        )
        # 未批准：不生效，报名早的 s_plain 优先
        self.make_session("S1", capacity=0)
        self.register("S1", "s_plain", at=T0)
        self.register("S1", "s_care", at=T1)
        result = self.cmd("set_capacity", {"session_id": "S1", "capacity": 1}, "cap-S1")
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["s_plain"])
        # 批准后：新评估中 s_care 优先
        approved = self.cmd("approve_care_grant", {"grant_id": "g1", "decided_by": "t1"}, "g1-ok")
        self.assertTrue(approved["ok"])
        self.make_session("S2", capacity=0)
        self.register("S2", "s_plain", at=T0)
        self.register("S2", "s_care", at=T1)
        result = self.cmd("set_capacity", {"session_id": "S2", "capacity": 1}, "cap-S2")
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["s_care"])
        snapshot = self.service.queue_snapshot("S2")
        self.assertTrue(snapshot["offered"][0]["offer_rationale"]["care_applied"])
        # 撤回后不再生效
        self.cmd("revoke_care_grant", {"grant_id": "g1", "decided_by": "t1"}, "g1-off")
        self.make_session("S3", capacity=0)
        self.register("S3", "s_plain", at=T0)
        self.register("S3", "s_care", at=T1)
        result = self.cmd("set_capacity", {"session_id": "S3", "capacity": 1}, "cap-S3")
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["s_plain"])

    def test_expired_care_grant_is_ignored(self) -> None:
        self.enroll("s_care")
        self.enroll("s_plain")
        self.cmd(
            "submit_care_grant",
            {
                "grant_id": "g-exp",
                "student_id": "s_care",
                "tier": 3,
                "detail": "敏感细节",
                "valid_until": "2020-01-01T00:00:00+00:00",
            },
            "g-exp",
        )
        self.cmd("approve_care_grant", {"grant_id": "g-exp"}, "g-exp-ok")
        self.make_session("S", capacity=0)
        self.register("S", "s_plain", at=T0)
        self.register("S", "s_care", at=T1)
        result = self.cmd("set_capacity", {"session_id": "S", "capacity": 1}, "cap-S")
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["s_plain"])

    def test_tie_broken_deterministically_by_student_id(self) -> None:
        """完全并列时按学号字典序，理由中标注决胜因子。"""
        self.enroll("s_b")
        self.enroll("s_a")
        self.make_session("S", capacity=0)
        self.register("S", "s_b", at=T0)
        self.register("S", "s_a", at=T0)
        snapshot = self.service.queue_snapshot("S")
        self.assertEqual([q["student_id"] for q in snapshot["queued"]], ["s_a", "s_b"])
        rationale = snapshot["queued"][0]["rationale"]
        self.assertEqual(rationale["tie_group_size"], 2)
        self.assertEqual(rationale["tie_broken_by"], "student_id")
        # 重读结果一致（确定性）
        again = self.service.queue_snapshot("S")
        self.assertEqual(
            [q["student_id"] for q in again["queued"]], ["s_a", "s_b"]
        )


class BackfillTest(ServiceTestCase):
    def test_withdraw_triggers_atomic_backfill_and_notification(self) -> None:
        """临时退出：同一事务内递补下一名，并生成通知记录。"""
        self.enroll("A")
        self.enroll("B")
        self.make_session("S", capacity=1)
        self.assertEqual(self.register("S", "A", at=T0)["status"], "offered")
        self.assertEqual(self.register("S", "B", at=T1)["status"], "queued")
        result = self.cmd(
            "withdraw_entry", {"session_id": "S", "student_id": "A"}, "wd-A"
        )
        self.assertTrue(result["freed_slot"])
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["B"])
        notification = result["notifications"][0]
        self.assertEqual(notification["status"], "sent")
        self.assertEqual(self.gateway.sent[-1], notification["notification_id"])
        # 送达确认：sent → delivered
        confirmed = self.cmd(
            "record_notification_result",
            {"notification_id": notification["notification_id"], "outcome": "delivered"},
            "ntf-1",
        )
        self.assertEqual(confirmed["status"], "delivered")
        snapshot = self.service.queue_snapshot("S")
        self.assertEqual(snapshot["offered"][0]["student_id"], "B")
        self.assertEqual(snapshot["offered"][0]["notification"]["status"], "delivered")

    def test_notification_dispatch_failure_is_recorded(self) -> None:
        """派发失败落库 failed，且不能重复登记送达结果。"""
        self.enroll("A")
        self.enroll("B")
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        self.gateway.fail_next = 1
        result = self.cmd("withdraw_entry", {"session_id": "S", "student_id": "A"}, "wd-A")
        notification = result["notifications"][0]
        self.assertEqual(notification["status"], "failed")
        again = self.cmd(
            "record_notification_result",
            {"notification_id": notification["notification_id"], "outcome": "delivered"},
            "ntf-bad",
        )
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"]["code"], "notification_state_error")

    def test_replay_withdraw_does_not_backfill_twice(self) -> None:
        """重放同一退出事件：返回首次结果，不会把名额再补给下一人。"""
        for sid in ("A", "B", "C"):
            self.enroll(sid)
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        self.register("S", "C", at=T2)
        first = self.cmd("withdraw_entry", {"session_id": "S", "student_id": "A"}, "wd-A")
        self.assertEqual([p["student_id"] for p in first["promoted"]], ["B"])
        replay = self.cmd("withdraw_entry", {"session_id": "S", "student_id": "A"}, "wd-A")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay, {**first, "replayed": True})
        state = self.statuses("S")
        self.assertEqual(state["offered"], ["B"])
        self.assertEqual(state["queued"], ["C"])


class IdempotencyTest(ServiceTestCase):
    def test_replay_register_returns_same_entry_without_duplicate(self) -> None:
        self.enroll("A")
        self.make_session("S", capacity=1)
        first = self.register("S", "A", event_id="evt-1")
        self.assertTrue(first["ok"])
        replay = self.register("S", "A", event_id="evt-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entry_id"], first["entry_id"])
        # 不同 event_id 的重复报名被唯一索引拦截
        conflict = self.register("S", "A", event_id="evt-2")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "already_registered")
        self.assertEqual(self.entry_count("S"), 1)

    def test_error_result_is_recorded_and_replayed(self) -> None:
        """失败同样幂等：同一 event_id 重放得到同一错误，换新事件才能成功。"""
        self.make_student("A")
        self.make_session("S", capacity=1)
        denied = self.register("S", "A", event_id="evt-no-consent")
        self.assertEqual(denied["error"]["code"], "consent_required")
        self.make_consent("A")
        replay = self.register("S", "A", event_id="evt-no-consent")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["error"]["code"], "consent_required")
        fresh = self.register("S", "A", event_id="evt-with-consent")
        self.assertTrue(fresh["ok"])

    def test_replay_check_in_is_single_occupancy(self) -> None:
        self.enroll("A")
        self.make_session("S", capacity=1)
        self.register("S", "A")
        first = self.cmd("check_in", {"session_id": "S", "student_id": "A"}, "ci-1")
        self.assertFalse(first["already_checked_in"])
        replay = self.cmd("check_in", {"session_id": "S", "student_id": "A"}, "ci-1")
        self.assertTrue(replay["replayed"])
        repeat = self.cmd("check_in", {"session_id": "S", "student_id": "A"}, "ci-2")
        self.assertTrue(repeat["already_checked_in"])
        self.assertEqual(self.entry_count("S"), 1)


class ConsentWithdrawalTest(ServiceTestCase):
    def test_withdraw_consent_removes_entries_and_backfills(self) -> None:
        self.enroll("A")
        self.enroll("B")
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        result = self.cmd("withdraw_consent", {"student_id": "A"}, "wd-consent-A")
        self.assertEqual(result["consent"], "withdrawn")
        self.assertEqual(len(result["removed"]), 1)
        self.assertEqual(
            [p["student_id"] for p in result["promotions"]["S"]], ["B"]
        )
        # 授权撤回后不能再报名；重新授权后恢复
        denied = self.register("S", "A", event_id="reg-A-again")
        self.assertEqual(denied["error"]["code"], "consent_required")
        self.cmd("grant_consent", {"student_id": "A"}, "consent-A-2")
        self.assertTrue(self.register("S", "A", event_id="reg-A-3")["ok"])

    def test_withdraw_consent_flags_checked_in_but_keeps_record(self) -> None:
        """已签到者不被抹除，标记给工作人员；其名额不重复递补。"""
        self.enroll("A")
        self.enroll("B")
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        self.cmd("check_in", {"session_id": "S", "student_id": "A"}, "ci-A")
        result = self.cmd("withdraw_consent", {"student_id": "A"}, "wd-consent-A")
        self.assertEqual(result["removed"], [])
        self.assertEqual(len(result["flagged_checked_in"]), 1)
        self.assertEqual(result["promotions"], {})
        state = self.statuses("S")
        self.assertEqual(state["offered"], ["A"])  # 签到记录保留
        self.assertEqual(state["queued"], ["B"])


class CapacityTest(ServiceTestCase):
    def test_expansion_promotes_in_order(self) -> None:
        for sid in ("A", "B", "C"):
            self.enroll(sid)
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        self.register("S", "C", at=T2)
        result = self.cmd("set_capacity", {"session_id": "S", "capacity": 3}, "cap-3")
        self.assertEqual([p["student_id"] for p in result["promoted"]], ["B", "C"])
        self.assertEqual(len(result["notifications"]), 2)

    def test_shrink_below_occupied_is_rejected(self) -> None:
        for sid in ("A", "B"):
            self.enroll(sid)
        self.make_session("S", capacity=2)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        rejected = self.cmd("set_capacity", {"session_id": "S", "capacity": 1}, "cap-1")
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "capacity_below_occupied")
        # 缩到与占用持平是允许的，但不触发递补
        even = self.cmd("set_capacity", {"session_id": "S", "capacity": 2}, "cap-2")
        self.assertTrue(even["ok"])
        self.assertEqual(even["promoted"], [])


class TransferTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        for sid in ("A", "B"):
            self.enroll(sid)
        self.make_session("S1", capacity=1)
        self.make_session("S2", capacity=1)
        self.register("S1", "A", at=T0)
        self.register("S1", "B", at=T1)

    def test_transfer_moves_entry_and_backfills_source(self) -> None:
        """跨场转移：源场次空位即时递补，目标场次保留原始报名时间。"""
        result = self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S2"},
            "tr-A",
        )
        self.assertEqual([p["student_id"] for p in result["from"]["promoted"]], ["B"])
        self.assertEqual(result["to"]["status"], "offered")
        self.assertEqual(result["to"]["registered_at"], "2026-09-30T08:00:00.000000+00:00")
        self.assertEqual(self.statuses("S1")["offered"], ["B"])
        self.assertEqual(self.statuses("S2")["offered"], ["A"])

    def test_transfer_keeps_seniority_in_target_queue(self) -> None:
        """转移到目标场次后按保留的原始报名时间排队，可排在先报名者之前。"""
        self.enroll("D")
        self.cmd("set_capacity", {"session_id": "S2", "capacity": 0}, "cap-S2-0")
        self.register("S2", "D", at=T2)  # D 在目标场次排队，报名时间晚于 A 的 T0
        self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S2"},
            "tr-A",
        )
        promoted = self.cmd("set_capacity", {"session_id": "S2", "capacity": 1}, "cap-S2-1")
        self.assertEqual([p["student_id"] for p in promoted["promoted"]], ["A"])
        self.assertEqual(self.statuses("S2")["queued"], ["D"])

    def test_transfer_rejects_ineligible_class_and_duplicate(self) -> None:
        self.make_session("S3", capacity=1, class_id="C2")
        denied = self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S3"},
            "tr-bad-class",
        )
        self.assertEqual(denied["error"]["code"], "not_eligible")
        same = self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S1"},
            "tr-same",
        )
        self.assertEqual(same["error"]["code"], "validation_error")
        # 目标场次已有有效记录：整体回滚，源场次记录不受影响
        self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S2"},
            "tr-A",
        )
        self.register("S1", "A", at=T2, event_id="reg-A-S1-again")
        duplicate = self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S2"},
            "tr-dup",
        )
        self.assertEqual(duplicate["error"]["code"], "already_registered")
        self.assertEqual(self.entry_count("S1"), 2)  # B 占位 + A 排队，源记录未丢

    def test_checked_in_entry_cannot_transfer(self) -> None:
        self.cmd("check_in", {"session_id": "S1", "student_id": "A"}, "ci-A")
        result = self.cmd(
            "transfer",
            {"student_id": "A", "from_session_id": "S1", "to_session_id": "S2"},
            "tr-A",
        )
        self.assertEqual(result["error"]["code"], "entry_not_transferable")


class CheckInAndCompletionTest(ServiceTestCase):
    def test_check_in_rules_and_history_accounting(self) -> None:
        self.enroll("A")
        self.enroll("B")
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        # 未获名额不能签到
        denied = self.cmd("check_in", {"session_id": "S", "student_id": "B"}, "ci-B")
        self.assertEqual(denied["error"]["code"], "not_offered")
        # 正常签到 → 结算后计入历史参与
        self.assertTrue(self.cmd("check_in", {"session_id": "S", "student_id": "A"}, "ci-A")["ok"])
        done = self.cmd("complete_session", {"session_id": "S"}, "done-S")
        self.assertEqual((done["completed"], done["no_show"], done["released"]), (1, 0, 1))
        # 已结束场次拒绝新报名
        closed = self.register("S", "B", event_id="reg-closed")
        self.assertEqual(closed["error"]["code"], "session_closed")
        # 历史参与影响下一场排序
        self.enroll("C")
        self.make_session("S2", capacity=0)
        self.register("S2", "A", at=T0)
        self.register("S2", "C", at=T1)
        promoted = self.cmd("set_capacity", {"session_id": "S2", "capacity": 1}, "cap-S2")
        self.assertEqual([p["student_id"] for p in promoted["promoted"]], ["C"])

    def test_complete_session_marks_no_show(self) -> None:
        self.enroll("A")
        self.enroll("B")
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T0)
        self.register("S", "B", at=T1)
        done = self.cmd("complete_session", {"session_id": "S"}, "done-S")
        self.assertEqual((done["completed"], done["no_show"], done["released"]), (0, 1, 1))


class ConcurrencyTest(ServiceTestCase):
    def _parallel(self, count, fn):
        barrier = threading.Barrier(count)
        results = [None] * count

        def worker(i):
            barrier.wait(timeout=10)
            results[i] = fn(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertNotIn(None, results)
        return results

    def test_concurrent_registration_never_overbooks(self) -> None:
        """8 人同时抢 3 个名额：恰好 3 人获得，无人重复占位。"""
        students = [f"s{i}" for i in range(8)]
        for sid in students:
            self.enroll(sid)
        self.make_session("S", capacity=3)
        results = self._parallel(
            8, lambda i: self.register("S", students[i], event_id=f"conc-{i}")
        )
        self.assertTrue(all(r["ok"] for r in results))
        state = self.statuses("S")
        self.assertEqual(len(state["offered"]), 3)
        self.assertEqual(len(set(state["offered"])), 3)
        self.assertEqual(len(state["queued"]), 5)

    def test_concurrent_same_student_registers_once(self) -> None:
        self.enroll("A")
        self.make_session("S", capacity=1)
        results = self._parallel(
            4, lambda i: self.register("S", "A", event_id=f"dup-{i}")
        )
        succeeded = [r for r in results if r["ok"]]
        failed = [r for r in results if not r["ok"]]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(failed), 3)
        self.assertTrue(all(r["error"]["code"] == "already_registered" for r in failed))
        self.assertEqual(self.entry_count("S"), 1)

    def test_concurrent_check_in_is_idempotent_per_student(self) -> None:
        """同时签到：数据库串行化后只有第一次生效，其余为确定性空操作。"""
        self.enroll("A")
        self.make_session("S", capacity=1)
        self.register("S", "A")
        results = self._parallel(
            4,
            lambda i: self.cmd(
                "check_in", {"session_id": "S", "student_id": "A"}, f"ci-conc-{i}"
            ),
        )
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sum(1 for r in results if not r["already_checked_in"]), 1)
        with self.service.db.read() as conn:
            checked = conn.execute(
                "SELECT COUNT(*) AS c FROM entries WHERE session_id = 'S'"
                " AND status = 'checked_in'"
            ).fetchone()["c"]
        self.assertEqual(checked, 1)


class PrivacyViewTest(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_student("A", contact="13800000000")
        self.make_consent("A")
        self.enroll("B")
        self.cmd(
            "submit_care_grant",
            {"grant_id": "g1", "student_id": "A", "tier": 2, "detail": "离异家庭母亲重病"},
            "g1",
        )
        self.cmd("approve_care_grant", {"grant_id": "g1"}, "g1-ok")
        self.make_session("S", capacity=1)
        self.register("S", "A", at=T1)
        self.register("S", "B", at=T0)

    def test_teacher_view_has_rationale_without_sensitive_fields(self) -> None:
        view = views.teacher_view(self.service.queue_snapshot("S"))
        # 教师能看到排序理由：A 因经批准的照顾排在名额位
        self.assertEqual(view["offered"][0]["student_id"], "A")
        rationale = view["offered"][0]["offer_rationale"]
        self.assertTrue(rationale["care_applied"])
        self.assertEqual(view["queued"][0]["student_id"], "B")
        self.assertIn("rule", view)
        # 但看不到任何敏感信息
        blob = json.dumps(view, ensure_ascii=False)
        for leaked in ("13800000000", "离异家庭", "guardian_contact", "detail"):
            self.assertNotIn(leaked, blob)

    def test_volunteer_view_is_minimal_roster(self) -> None:
        view = views.volunteer_view(self.service.queue_snapshot("S"))
        self.assertEqual(len(view["roster"]), 1)
        self.assertEqual(set(view["roster"][0]), {"name", "status", "checked_in_at"})
        self.assertNotIn("rationale", json.dumps(view))

    def test_student_view_shows_only_self(self) -> None:
        snapshot = self.service.queue_snapshot("S")
        own = views.student_view(snapshot, "B")
        self.assertEqual(own["section"], "queued")
        self.assertEqual(own["rank"], 1)
        blob = json.dumps(own, ensure_ascii=False)
        self.assertNotIn("学生A", blob)  # 看不到他人姓名
        self.assertNotIn("name", own)
        stranger = views.student_view(snapshot, "nobody")
        self.assertEqual(stranger["status"], "not_registered")


if __name__ == "__main__":
    unittest.main()
