"""确定性公平排序的单元测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation_queue.ranking import SignupView, build_ranking, rank_key

T = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)


def view(sid, *, at=T, history=0, accom=None, transferred=False, consent=True):
    return SignupView(
        student_id=sid,
        signed_up_at=at,
        history_count=history,
        consent_active=consent,
        accommodation_level=accom,
        accommodation_reason="medical" if accom == 0 else None,
        transferred_in=transferred,
    )


class RankingTest(unittest.TestCase):
    def test_history_fewer_first(self) -> None:
        entries = [view("a", history=2), view("b", history=0), view("c", history=1)]
        result = build_ranking(entries, capacity=3)
        self.assertEqual([e.student_id for e in result], ["b", "c", "a"])

    def test_accommodation_breaks_tie_before_signup_time(self) -> None:
        # 历史相同：持依据者优先，即使报名更晚
        entries = [
            view("late_medical", at=T.replace(second=59), accom=0),
            view("early_plain", at=T),
        ]
        result = build_ranking(entries, capacity=2)
        self.assertEqual(result[0].student_id, "late_medical")
        self.assertIn("照顾依据", result[0].reason_text)

    def test_student_id_is_final_tie_breaker_and_deterministic(self) -> None:
        # 业务因素完全相同 => 并列，按学生 ID 决胜，两次运行结果一致
        entries = [view("z"), view("a"), view("m")]
        first = build_ranking(entries, capacity=3)
        second = build_ranking(list(reversed(entries)), capacity=3)
        ids_first = [e.student_id for e in first]
        ids_second = [e.student_id for e in second]
        self.assertEqual(ids_first, ["a", "m", "z"])
        self.assertEqual(ids_first, ids_second)
        self.assertTrue(second[1].factors["tied_with_previous"])

    def test_capacity_marks_selected_and_waitlisted(self) -> None:
        entries = [view(s) for s in "abcd"]
        result = build_ranking(entries, capacity=2)
        self.assertEqual([e.selected for e in result], [True, True, False, False])

    def test_consent_withdrawn_excluded(self) -> None:
        entries = [view("ok"), view("blocked", consent=False)]
        result = build_ranking(entries, capacity=2)
        self.assertEqual([e.student_id for e in result], ["ok"])

    def test_transfer_keeps_original_enqueue_time(self) -> None:
        entries = [
            view("normal", at=T.replace(day=21)),
            view("transfer", at=T, transferred=True),
        ]
        result = build_ranking(entries, capacity=2)
        self.assertEqual(result[0].student_id, "transfer")
        self.assertTrue(result[0].factors["transferred_in"])

    def test_rank_key_pure_and_orderable(self) -> None:
        a = view("a", history=0)
        b = view("b", history=1)
        self.assertLess(rank_key(a), rank_key(b))


if __name__ == "__main__":
    unittest.main()
