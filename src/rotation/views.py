"""按角色裁剪的队列视图。

隐私边界：班主任（teacher）能看到每位学生的排序理由与通知送达状态，
但看不到监护联系方式、照顾依据细节等与排序无关的敏感信息；
活动志愿者（volunteer）只拿到签到名单；学生只能看到自己的位置。
"""
from __future__ import annotations

from .policy import RULE

SESSION_PUBLIC_KEYS = ("session_id", "title", "class_id", "capacity", "state", "starts_at")


def teacher_view(snapshot: dict) -> dict:
    """班主任视图：完整排序理由 + 通知送达状态，无敏感字段。"""
    return {
        "session": {k: snapshot["session"][k] for k in SESSION_PUBLIC_KEYS},
        "rule": RULE,
        "offered": [
            {
                "position": item["position"],
                "student_id": item["student_id"],
                "name": item["name"],
                "class_id": item["class_id"],
                "status": item["status"],
                "offered_at": item["offered_at"],
                "checked_in_at": item["checked_in_at"],
                "offer_rationale": item["offer_rationale"],
                "notification": item["notification"],
            }
            for item in snapshot["offered"]
        ],
        "queued": [
            {
                "rank": item["rank"],
                "student_id": item["student_id"],
                "name": item["name"],
                "class_id": item["class_id"],
                "status": item["status"],
                "rationale": item["rationale"],
            }
            for item in snapshot["queued"]
        ],
    }


def volunteer_view(snapshot: dict) -> dict:
    """活动志愿者视图：仅签到所需的名单与状态。"""
    return {
        "session": {
            k: snapshot["session"][k] for k in ("session_id", "title", "starts_at", "state")
        },
        "roster": [
            {
                "name": item["name"],
                "status": item["status"],
                "checked_in_at": item["checked_in_at"],
            }
            for item in snapshot["offered"]
        ],
    }


def student_view(snapshot: dict, student_id: str) -> dict:
    """学生视图：只看得到自己的位置、排序理由与通知状态。"""
    for item in snapshot["offered"]:
        if item["student_id"] == student_id:
            return {
                "session_id": snapshot["session"]["session_id"],
                "section": "offered",
                "position": item["position"],
                "status": item["status"],
                "offer_rationale": item["offer_rationale"],
                "notification": item["notification"],
            }
    for item in snapshot["queued"]:
        if item["student_id"] == student_id:
            return {
                "session_id": snapshot["session"]["session_id"],
                "section": "queued",
                "rank": item["rank"],
                "status": item["status"],
                "rationale": item["rationale"],
            }
    return {
        "session_id": snapshot["session"]["session_id"],
        "section": None,
        "status": "not_registered",
    }
