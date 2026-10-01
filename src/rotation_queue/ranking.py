"""确定性公平排序。

排序键（全部升序，全部可解释）：

1. 监护授权：授权有效者优先（撤回者直接出局，不参与排序）。
2. 班级内历史参与次数：参加得越少越靠前，避免"有人连续参加"。
3. 经批准且在有效期内的照顾依据：有依据者优先；按依据等级
   （医疗 > 其他）再细分，同级之间不互相插队。
4. 本场报名时间：先报者优先。
5. 学生 ID：最终决胜键，保证并列者也有全局确定顺序，永不依赖字典序之外的随机因素。

每一步比较都会记录到 Rationale，教师端可见"为什么我在这个位置"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 照顾依据等级：数值越小越优先；未列出的类型归入 OTHER。
ACCOMMODATION_RANK = {
    "medical": 0,
    "accessibility": 1,
    "other": 2,
}
NO_ACCOMMODATION = 9


@dataclass(frozen=True)
class SignupView:
    """排序输入：聚合投影给出的学生快照（不含敏感明细）。"""

    student_id: str
    signed_up_at: datetime
    history_count: int
    consent_active: bool
    accommodation_level: int | None  # None = 无有效依据
    accommodation_reason: str | None  # 仅编码，不含医疗细节
    transferred_in: bool = False


@dataclass
class RankedEntry:
    student_id: str
    rank_key: tuple
    position: int
    selected: bool
    factors: dict[str, Any] = field(default_factory=dict)
    reason_codes: list[str] = field(default_factory=list)
    reason_text: str = ""


def level_for(reason_type: str | None) -> int | None:
    if reason_type is None:
        return None
    return ACCOMMODATION_RANK.get(reason_type, ACCOMMODATION_RANK["other"])


def rank_key(view: SignupView) -> tuple:
    """纯函数排序键。传入视图必须已确认 consent_active=True。"""
    accom = view.accommodation_level if view.accommodation_level is not None else NO_ACCOMMODATION
    return (
        view.history_count,
        accom,
        view.signed_up_at,
        # transferred_in 作为时间戳相同时的稳定次级键，不额外插队
        view.student_id,
    )


def factor_text(view: SignupView) -> tuple[list[str], str]:
    codes: list[str] = []
    parts: list[str] = []
    codes.append(f"history={view.history_count}")
    parts.append(f"历史参与 {view.history_count} 次")
    if view.accommodation_level is not None:
        codes.append("accommodation=approved")
        parts.append("持有效经批准照顾依据")
    else:
        codes.append("accommodation=none")
    enq = view.signed_up_at
    codes.append(f"enqueued_at={enq.isoformat()}")
    if view.transferred_in:
        parts.append(f"跨场转入，沿用原报名时间 {enq.isoformat()}")
    else:
        parts.append(f"报名时间 {enq.isoformat()}")
    return codes, "；".join(parts)


def build_ranking(
    entries: list[SignupView],
    capacity: int,
) -> list[RankedEntry]:
    """对已通过资格过滤（授权有效）的报名者生成带解释的确定性名次。

    capacity 为发布时刻的有效容量（含临时扩容）。并列者由学生 ID
    决胜，因此任何两个输入集合的相对顺序与执行节点、时间无关。
    """
    eligible = [v for v in entries if v.consent_active]
    ordered = sorted(eligible, key=rank_key)
    result: list[RankedEntry] = []
    prev_key: tuple | None = None
    tie_group = 0
    for idx, view in enumerate(ordered):
        key = rank_key(view)
        # 与前一名在"业务因素"（去掉最终 ID 决胜键）上完全相同 => 并列
        business_key = key[:-1]
        if prev_key is not None and prev_key[:-1] == business_key:
            tie_group += 1
        else:
            tie_group = 0
        codes, text = factor_text(view)
        selected = idx < capacity
        if tie_group > 0:
            text += "；与前一名业务条件相同，按学生编号决胜"
        text += f"；名次 {idx + 1}，容量 {capacity}，" + ("获得名额" if selected else "进入候补")
        result.append(
            RankedEntry(
                student_id=view.student_id,
                rank_key=key,
                position=idx + 1,
                selected=selected,
                factors={
                    "history_count": view.history_count,
                    "accommodation_level": view.accommodation_level,
                    "enqueued_at": view.signed_up_at.isoformat(),
                    "transferred_in": view.transferred_in,
                    "tie_breaker_student_id": view.student_id,
                    "tied_with_previous": tie_group > 0,
                },
                reason_codes=codes,
                reason_text=text,
            )
        )
        prev_key = key
    return result


def next_waitlist(entries: list[SignupView], already_selected: set[str]) -> str | None:
    """原子递补时挑选下一名候补：同一排序规则在当前快照上重算。

    历史参与次数在签到完成后才增长，因此已入选者不会因递补重排被挤动。
    """
    eligible = [v for v in entries if v.consent_active and v.student_id not in already_selected]
    if not eligible:
        return None
    return sorted(eligible, key=rank_key)[0].student_id
