"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    http_status = 422

    def __init__(self, code: str, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if http_status is not None:
            self.http_status = http_status


class ValidationError(DomainError):
    http_status = 422


class NotFoundError(DomainError):
    http_status = 404


class ConflictError(DomainError):
    http_status = 409


class ForbiddenError(DomainError):
    http_status = 403
