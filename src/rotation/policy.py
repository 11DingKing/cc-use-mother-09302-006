"""公平排序策略：可解释、确定性的队列顺序。

排序规则（优先级从高到低）：
1. 经批准且在有效期内的照顾级别（级别高者优先）；
2. 历史参与次数（次数少者优先，避免有人连续参加、有人长期排不到）；
3. 报名时间（早报名者优先，跨场转移保留原始报名时间）；
4. 学号字典序（并列时的确定决胜因子，保证任何重放结果一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

RULE = "照顾级别（高优先）→ 历史参与次数（少优先）→ 报名时间（早优先）→ 学号（字典序）"


@dataclass(frozen=True)
class Factors:
    """单个排队者的排序因子快照。"""

    care_tier: int
    participation_count: int
    registered_at: str
    student_id: str


def sort_key(factors: Factors) -> tuple:
    """升序即优先：照顾级别取负，其余取原值，学号兜底保证并列时结果确定。"""
    return (
        -factors.care_tier,
        factors.participation_count,
        factors.registered_at,
        factors.student_id,
    )


def tie_identity(factors: Factors) -> tuple:
    """并列组标识：除学号外的全部因子都相同即互为并列。"""
    return (factors.care_tier, factors.participation_count, factors.registered_at)


def tie_group_sizes(all_factors: Iterable[Factors]) -> dict[tuple, int]:
    """统计每个并列组的大小，用于在理由中标注是否动用了学号决胜。"""
    sizes: dict[tuple, int] = {}
    for factors in all_factors:
        key = tie_identity(factors)
        sizes[key] = sizes.get(key, 0) + 1
    return sizes


def explain(factors: Factors, *, tie_group_size: int) -> dict:
    """生成可展示给班主任的排序理由（不含照顾依据细节等敏感信息）。"""
    return {
        "rule": RULE,
        "care_applied": factors.care_tier > 0,
        "care_tier": factors.care_tier,
        "participation_count": factors.participation_count,
        "registered_at": factors.registered_at,
        "tie_group_size": tie_group_size,
        "tie_broken_by": "student_id" if tie_group_size > 1 else None,
    }
