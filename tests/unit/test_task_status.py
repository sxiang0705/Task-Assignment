from __future__ import annotations

from datetime import date, datetime

import pytest

from task_assignment.domain.enums import ScheduleStatus, TaskStatus
from task_assignment.domain.models import ReviewSchedule
from task_assignment.domain.task_status import calculate_task_status, is_due_today, is_overdue


def schedule(
    sequence: int,
    when: datetime,
    status: ScheduleStatus = ScheduleStatus.PENDING,
) -> ReviewSchedule:
    return ReviewSchedule(
        id=sequence, task_id=1, sequence=sequence, scheduled_at=when, status=status
    )


def test_val_date_001_and_002_today_uses_local_date_not_time() -> None:
    """VAL-DATE-001/002: an earlier time today remains due today."""

    item = schedule(1, datetime(2026, 9, 5, 1, 0))

    assert is_due_today(item, date(2026, 9, 5))
    assert not is_overdue(item, date(2026, 9, 5))


def test_val_date_003_only_pending_items_before_today_are_overdue() -> None:
    """VAL-DATE-003: terminal items are excluded from overdue."""

    yesterday = datetime(2026, 9, 4, 23, 59)

    assert is_overdue(schedule(1, yesterday), date(2026, 9, 5))
    assert not is_overdue(schedule(2, yesterday, ScheduleStatus.COMPLETED), date(2026, 9, 5))
    assert not is_overdue(schedule(3, yesterday, ScheduleStatus.SKIPPED), date(2026, 9, 5))


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"is_archived": True, "is_paused": True}, TaskStatus.ARCHIVED),
        ({"is_paused": True}, TaskStatus.PAUSED),
    ],
)
def test_val_task_006_archived_and_paused_have_highest_priority(
    kwargs: dict[str, bool], expected: TaskStatus
) -> None:
    """VAL-TASK-006: archived outranks paused, which outranks schedule state."""

    result = calculate_task_status(
        start_at=datetime(2026, 1, 1),
        schedules=[schedule(1, datetime(2026, 1, 2), ScheduleStatus.COMPLETED)],
        today=date(2026, 9, 5),
        **kwargs,
    )

    assert result is expected


def test_val_task_006_terminal_statuses_follow_priority() -> None:
    completed = [schedule(1, datetime(2026, 1, 2), ScheduleStatus.COMPLETED)]
    skipped = [schedule(1, datetime(2026, 1, 2), ScheduleStatus.SKIPPED)]

    assert (
        calculate_task_status(
            start_at=datetime(2026, 1, 1), schedules=completed, today=date(2026, 9, 5)
        )
        is TaskStatus.COMPLETED
    )
    assert (
        calculate_task_status(
            start_at=datetime(2026, 1, 1), schedules=skipped, today=date(2026, 9, 5)
        )
        is TaskStatus.INCOMPLETE
    )


def test_val_task_006_due_statuses_and_temporal_fallbacks() -> None:
    today = date(2026, 9, 5)

    assert (
        calculate_task_status(
            start_at=datetime(2026, 1, 1),
            schedules=[schedule(1, datetime(2026, 9, 4, 20))],
            today=today,
        )
        is TaskStatus.OVERDUE
    )
    assert (
        calculate_task_status(
            start_at=datetime(2026, 1, 1),
            schedules=[schedule(1, datetime(2026, 9, 5, 1))],
            today=today,
        )
        is TaskStatus.DUE_TODAY
    )
    assert (
        calculate_task_status(
            start_at=datetime(2026, 9, 6),
            schedules=[schedule(1, datetime(2026, 9, 7))],
            today=today,
        )
        is TaskStatus.NOT_STARTED
    )
    assert (
        calculate_task_status(
            start_at=datetime(2026, 9, 1),
            schedules=[schedule(1, datetime(2026, 9, 7))],
            today=today,
        )
        is TaskStatus.IN_PROGRESS
    )
