"""后端领域错误类型。

所有可预期的业务失败都抛出 DomainError 子类：命令处理器会把它们
记录为确定性的错误结果（同一 event_id 重放得到同一答复），未预期的
异常则直接回滚、不留下任何事件记录。
"""
from __future__ import annotations


class DomainError(Exception):
    """可预期的领域错误，结果会被记录并用于幂等重放。"""

    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message


class ValidationError(DomainError):
    code = "validation_error"


class NotFound(DomainError):
    code = "not_found"


class SessionClosed(DomainError):
    code = "session_closed"


class NotEligible(DomainError):
    code = "not_eligible"


class ConsentRequired(DomainError):
    code = "consent_required"


class AlreadyRegistered(DomainError):
    code = "already_registered"


class EntryNotActive(DomainError):
    code = "entry_not_active"


class EntryNotTransferable(DomainError):
    code = "entry_not_transferable"


class NotOffered(DomainError):
    code = "not_offered"


class CapacityBelowOccupied(DomainError):
    code = "capacity_below_occupied"


class NotificationStateError(DomainError):
    code = "notification_state_error"


class GrantStateError(DomainError):
    code = "grant_state_error"
