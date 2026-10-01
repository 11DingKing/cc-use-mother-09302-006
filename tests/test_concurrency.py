"""并发压力测试：多连接同时操作时，原子递补不重复、不超员。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation_queue.service import QueueService
from rotation_queue.store import EventStore
from rotation_queue.errors import DomainError


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_cancellations_promote_exact_distinct_waitlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "q.db"

            def open_svc() -> QueueService:
                return QueueService(EventStore(db))

            boot = open_svc()
            boot.register_class({"class_id": "c1", "name": "班"})
            boot.schedule_session({"session_id": "s1", "class_id": "c1",
                                   "starts_at": "2026-10-05T10:00:00+00:00", "capacity": 2})
            for i, sid in enumerate("abcde"):
                boot.register_student({"student_id": sid, "class_id": "c1", "name": sid})
                boot.grant_consent({"student_id": sid})
                boot.sign_up({"session_id": "s1", "student_id": sid,
                              "enqueued_at": f"2026-09-20T09:0{i}:00+00:00"})
            boot.publish_roster({"session_id": "s1"})  # a,b 入选；c,d,e 候补

            errors: list[BaseException] = []

            def cancel(victim: str) -> None:
                try:
                    open_svc().cancel_signup({"session_id": "s1", "student_id": victim})
                except DomainError as exc:  # 并发下业务冲突是可接受响应，非崩溃
                    errors.append(exc)

            t1 = threading.Thread(target=cancel, args=("a",))
            t2 = threading.Thread(target=cancel, args=("b",))
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            final = open_svc()
            state = final.store.load_state()
            session = state.sessions["s1"]
            # 核心不变量：入选恰好 2 人、互不相同、都是候补池里的人、仍有效报名
            self.assertEqual(len(session.selected), 2)
            self.assertEqual(len(set(session.selected)), 2)
            self.assertTrue(set(session.selected).issubset({"c", "d", "e"}))
            for sid in session.selected:
                self.assertIn(sid, session.signups)
                self.assertTrue(state.students[sid].consent_active)

            # 每个入选者恰有一条 promoted 通知且不重复
            promo_students = [
                n.student_id for n in session.notifications.values() if n.kind == "promoted"
            ]
            self.assertEqual(sorted(promo_students), sorted(session.selected))

            # 事件流重放两次结果一致（确定性）
            again = final.store.load_state()
            self.assertEqual(again.sessions["s1"].selected, session.selected)


if __name__ == "__main__":
    unittest.main()
