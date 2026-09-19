"""Task status calculation using the documented priority order."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from task_assignment.domain.enums import ScheduleStatus, TaskStatus
from task_assignment.domain.models import ReviewSchedule


def calculate_task_status(
    *,
    start_at: datetime,
    schedules: Iterable[ReviewSchedule],
    today: date,
    is_archived: bool = False,
    is_paused: bool = False,
) -> TaskStatus:
    """Calculate status as archived > paused > terminal > due > temporal."""

    if is_archived:
        return TaskStatus.ARCHIVED
    if is_paused:
        return TaskStatus.PAUSED

    items = tuple(schedules)
    if items and all(item.status is ScheduleStatus.COMPLETED for item in items):
        return TaskStatus.COMPLETED

    pending = tuple(item for item in items if item.status is ScheduleStatus.PENDING)
    if not pending and any(item.status is ScheduleStatus.SKIPPED for item in items):
        return TaskStatus.INCOMPLETE
    if any(item.scheduled_at.date() < today for item in pending):
        return TaskStatus.OVERDUE
    if any(item.scheduled_at.date() == today for item in pending):
        return TaskStatus.DUE_TODAY
    if start_at.date() > today:
        return TaskStatus.NOT_STARTED
    return TaskStatus.IN_PROGRESS


def is_due_today(schedule: ReviewSchedule, today: date) -> bool:
    return schedule.status is ScheduleStatus.PENDING and schedule.scheduled_at.date() == today


def is_overdue(schedule: ReviewSchedule, today: date) -> bool:
    return schedule.status is ScheduleStatus.PENDING and schedule.scheduled_at.date() < today
