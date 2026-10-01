"""事件存储测试：磁盘持久化、重放确定性、跨进程序号单调。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation_queue.service import QueueService
from rotation_queue.store import EventStore


class PersistenceTest(unittest.TestCase):
    def test_reload_from_disk_rebuilds_identical_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "q.db"
            store = EventStore(db)
            svc = QueueService(store)
            svc.register_class({"class_id": "c1", "name": "班"})
            svc.schedule_session({"session_id": "s1", "class_id": "c1",
                                  "starts_at": "2026-10-05T10:00:00+00:00", "capacity": 2})
            for sid in "abc":
                svc.register_student({"student_id": sid, "class_id": "c1", "name": sid})
                svc.grant_consent({"student_id": sid})
                svc.sign_up({"session_id": "s1", "student_id": sid,
                             "enqueued_at": f"2026-09-20T09:0{ord(sid)-96}:00+00:00"})
            svc.publish_roster({"session_id": "s1"})
            svc.cancel_signup({"session_id": "s1", "student_id": "a"})
            view_before = svc.teacher_view("s1")
            store.close()

            # 重新打开：仅凭事件流重建，结果必须一致
            store2 = EventStore(db)
            svc2 = QueueService(store2)
            view_after = svc2.teacher_view("s1")
            self.assertEqual(
                [(r["student_id"], r["selected"]) for r in view_before["roster"]],
                [(r["student_id"], r["selected"]) for r in view_after["roster"]],
            )
            store2.close()

    def test_event_sequences_are_monotonic(self) -> None:
        store = EventStore(":memory:")
        svc = QueueService(store)
        svc.register_class({"class_id": "c1", "name": "班"})
        svc.schedule_session({"session_id": "s1", "class_id": "c1",
                              "starts_at": "2026-10-05T10:00:00+00:00", "capacity": 1})
        svc.register_student({"student_id": "a", "class_id": "c1", "name": "a"})
        svc.grant_consent({"student_id": "a"})
        svc.sign_up({"session_id": "s1", "student_id": "a"})
        seqs = [e.sequence for e in store.list_events()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


if __name__ == "__main__":
    unittest.main()
