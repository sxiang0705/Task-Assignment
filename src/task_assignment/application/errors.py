"""User-facing application service errors."""

from __future__ import annotations


class ServiceError(RuntimeError):
    """Base error exposed by services instead of persistence exceptions."""

    code = "operation_failed"

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class ServiceValidationError(ServiceError):
    code = "validation_error"


class ServiceNotFoundError(ServiceError):
    code = "not_found"


class ServiceConflictError(ServiceError):
    code = "conflict"
