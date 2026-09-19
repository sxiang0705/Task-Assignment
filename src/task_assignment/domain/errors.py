"""Domain-level errors safe to translate into user-facing messages."""


class DomainError(ValueError):
    """Base class for rejected Task Assignment business operations."""


class ScheduleValidationError(DomainError):
    """Raised when a proposed review schedule violates domain rules."""


class ReviewTransitionError(DomainError):
    """Raised when a terminal review item is changed again."""
