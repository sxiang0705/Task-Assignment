"""Review completion metrics."""

from __future__ import annotations

from collections.abc import Iterable

from task_assignment.domain.enums import ScheduleStatus
from task_assignment.domain.models import ReviewSchedule


def completion_rate(schedules: Iterable[ReviewSchedule]) -> float:
    """Return completed / all schedules as a percentage.

    Skipped reviews remain in the denominator and never enter the numerator.
    """

    items = tuple(schedules)
    if not items:
        return 0.0
    completed = sum(item.status is ScheduleStatus.COMPLETED for item in items)
    return completed / len(items) * 100.0
