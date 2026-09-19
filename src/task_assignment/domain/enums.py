"""Stable values persisted or exchanged by the domain layer."""

from __future__ import annotations

from enum import StrEnum


class ScheduleMode(StrEnum):
    CURVE = "curve"
    MANUAL = "manual"


class ScheduleStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ReviewAction(StrEnum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    POSTPONED = "postponed"
    MERGED = "merged"
