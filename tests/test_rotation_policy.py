"""公平排序策略的单元测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation.policy import Factors, explain, sort_key, tie_group_sizes, tie_identity

T1 = "2026-09-30T08:00:00.000000+00:00"
T2 = "2026-09-30T09:00:00.000000+00:00"


def factors(student_id, *, tier=0, count=0, at=T1):
    return Factors(
        care_tier=tier, participation_count=count, registered_at=at, student_id=student_id
    )


class SortKeyTest(unittest.TestCase):
    def test_care_tier_outranks_everything(self) -> None:
        cared = factors("s2", tier=1, count=9, at=T2)
        plain = factors("s1", tier=0, count=0, at=T1)
        self.assertLess(sort_key(cared), sort_key(plain))

    def test_fewer_participations_first(self) -> None:
        never = factors("s2", count=0, at=T2)
        veteran = factors("s1", count=3, at=T1)
        self.assertLess(sort_key(never), sort_key(veteran))

    def test_earlier_registration_first(self) -> None:
        early = factors("s2", at=T1)
        late = factors("s1", at=T2)
        self.assertLess(sort_key(early), sort_key(late))

    def test_full_tie_broken_by_student_id(self) -> None:
        a = factors("s_a")
        b = factors("s_b")
        self.assertLess(sort_key(a), sort_key(b))
        # 确定性：交换构造顺序结果不变
        self.assertEqual(
            sorted([sort_key(b), sort_key(a)]), [sort_key(a), sort_key(b)]
        )


class TieGroupTest(unittest.TestCase):
    def test_tie_group_sizes(self) -> None:
        group = [factors("s_a"), factors("s_b"), factors("s_c", at=T2)]
        sizes = tie_group_sizes(group)
        self.assertEqual(sizes[tie_identity(factors("s_a"))], 2)
        self.assertEqual(sizes[tie_identity(factors("s_c", at=T2))], 1)

    def test_explain_marks_tie_break(self) -> None:
        tied = explain(factors("s_a"), tie_group_size=3)
        self.assertEqual(tied["tie_broken_by"], "student_id")
        self.assertEqual(tied["tie_group_size"], 3)
        clean = explain(factors("s_a"), tie_group_size=1)
        self.assertIsNone(clean["tie_broken_by"])

    def test_explain_contains_no_sensitive_fields(self) -> None:
        rationale = explain(factors("s_a", tier=2), tie_group_size=1)
        self.assertTrue(rationale["care_applied"])
        self.assertEqual(rationale["care_tier"], 2)
        self.assertNotIn("detail", rationale)


if __name__ == "__main__":
    unittest.main()
