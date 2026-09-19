"""Pure domain models without UI or persistence dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from task_assignment.domain.enums import (
    ReviewAction,
    ScheduleMode,
    ScheduleStatus,
    TaskStatus,
)


@dataclass(frozen=True, slots=True)
class Task:
    name: str
    start_at: datetime
    schedule_mode: ScheduleMode
    review_count: int | None
    id: int | None = None
    description: str = ""
    category_id: int | None = None
    status: TaskStatus = TaskStatus.NOT_STARTED
    completion_rate: float = 0.0
    is_paused: bool = False
    paused_on: date | None = None
    is_archived: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReviewSchedule:
    task_id: int
    sequence: int
    scheduled_at: datetime
    status: ScheduleStatus = ScheduleStatus.PENDING
    id: int | None = None
    processed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    task_id: int
    schedule_id: int | None
    action: ReviewAction
    occurred_at: datetime
    previous_scheduled_at: datetime | None = None
    resulting_scheduled_at: datetime | None = None
    merged_into_schedule_id: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewTransition:
    schedule: ReviewSchedule
    record: ReviewRecord


@dataclass(frozen=True, slots=True)
class PostponeResult:
    schedules: tuple[ReviewSchedule, ...]
    records: tuple[ReviewRecord, ...]
    removed_schedule_ids: tuple[int, ...]
