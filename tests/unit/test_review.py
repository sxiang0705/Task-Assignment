from __future__ import annotations

from datetime import date, datetime

import pytest

from task_assignment.domain.enums import ReviewAction, ScheduleStatus
from task_assignment.domain.errors import ReviewTransitionError, ScheduleValidationError
from task_assignment.domain.models import ReviewSchedule
from task_assignment.domain.progress import completion_rate
from task_assignment.domain.review import (
    complete_review,
    postpone_to_tomorrow,
    rebuild_pending_schedules,
    resume_after_pause,
    skip_review,
)


def item(
    item_id: int,
    sequence: int,
    day: int,
    *,
    status: ScheduleStatus = ScheduleStatus.PENDING,
    hour: int = 9,
) -> ReviewSchedule:
    return ReviewSchedule(
        id=item_id,
        task_id=7,
        sequence=sequence,
        scheduled_at=datetime(2026, 1, day, hour, 30),
        status=status,
    )


def test_val_review_002_complete_creates_history_and_terminal_state() -> None:
    """VAL-REVIEW-002: completion updates state and creates a record."""

    occurred_at = datetime(2026, 1, 2, 12)
    transition = complete_review(item(1, 1, 2), occurred_at)

    assert transition.schedule.status is ScheduleStatus.COMPLETED
    assert transition.schedule.processed_at == occurred_at
    assert transition.record.action is ReviewAction.COMPLETED


def test_val_review_004_skip_is_terminal_and_irreversible() -> None:
    """VAL-REVIEW-004: skipped items cannot later be completed or skipped."""

    occurred_at = datetime(2026, 1, 2, 12)
    skipped = skip_review(item(1, 1, 2), occurred_at).schedule

    assert skipped.status is ScheduleStatus.SKIPPED
    with pytest.raises(ReviewTransitionError):
        complete_review(skipped, occurred_at)
    with pytest.raises(ReviewTransitionError):
        skip_review(skipped, occurred_at)


def test_val_review_005_skipped_items_stay_in_completion_denominator() -> None:
    """VAL-REVIEW-005: completed / all includes skipped in all."""

    schedules = [
        item(1, 1, 2, status=ScheduleStatus.COMPLETED),
        item(2, 2, 3, status=ScheduleStatus.SKIPPED),
        item(3, 3, 4),
        item(4, 4, 5, status=ScheduleStatus.COMPLETED),
    ]

    assert completion_rate(schedules) == 50.0
    assert completion_rate([]) == 0.0


def test_val_review_007_and_008_postpone_shifts_only_current_and_later_pending() -> None:
    """VAL-REVIEW-007/008: postpone preserves times and terminal entries."""

    schedules = [
        item(1, 1, 1),
        item(2, 2, 2),
        item(3, 3, 3, status=ScheduleStatus.COMPLETED),
        item(4, 4, 4, status=ScheduleStatus.SKIPPED),
        item(5, 5, 5),
    ]

    result = postpone_to_tomorrow(
        schedules,
        current_schedule_id=2,
        occurred_at=datetime(2026, 1, 2, 10),
    )
    by_id = {schedule.id: schedule for schedule in result.schedules}

    assert by_id[1].scheduled_at == datetime(2026, 1, 1, 9, 30)
    assert by_id[2].scheduled_at == datetime(2026, 1, 3, 9, 30)
    assert by_id[3] == schedules[2]
    assert by_id[4] == schedules[3]
    assert by_id[5].scheduled_at == datetime(2026, 1, 6, 9, 30)


def test_val_review_009_postpone_merges_duplicate_pending_times_with_history() -> None:
    """VAL-REVIEW-009: collisions reduce schedule count and create merge history."""

    schedules = [
        item(1, 1, 4),
        item(2, 2, 2),
        item(3, 3, 3),
    ]

    result = postpone_to_tomorrow(
        schedules,
        current_schedule_id=2,
        occurred_at=datetime(2026, 1, 2, 10),
    )

    assert len(result.schedules) == 2
    assert result.removed_schedule_ids == (3,)
    merge_record = next(record for record in result.records if record.action is ReviewAction.MERGED)
    assert merge_record.schedule_id == 3
    assert merge_record.merged_into_schedule_id == 1


def test_val_task_009_resume_shifts_only_unprocessed_by_paused_calendar_days() -> None:
    """VAL-TASK-009: resume shifts pending entries across month boundaries."""

    schedules = [
        item(1, 1, 30),
        item(2, 2, 31, status=ScheduleStatus.COMPLETED),
        item(3, 3, 31, status=ScheduleStatus.SKIPPED),
    ]

    resumed = resume_after_pause(
        schedules,
        paused_on=date(2026, 1, 30),
        resumed_on=date(2026, 2, 2),
    )

    assert resumed[0].scheduled_at == datetime(2026, 2, 2, 9, 30)
    assert resumed[1:] == tuple(schedules[1:])


def test_val_schedule_008_rebuild_preserves_completed_and_skipped_history() -> None:
    """VAL-SCHEDULE-008: only pending entries are rebuilt."""

    completed = item(1, 1, 2, status=ScheduleStatus.COMPLETED)
    skipped = item(2, 2, 3, status=ScheduleStatus.SKIPPED)
    pending = item(3, 3, 4)
    proposed = [datetime(2026, 2, day, 9, 30) for day in (2, 3, 7, 14)]

    rebuilt = rebuild_pending_schedules(
        [completed, skipped, pending],
        proposed,
        task_id=7,
        start_at=datetime(2026, 2, 1, 9, 30),
    )

    assert completed in rebuilt
    assert skipped in rebuilt
    assert pending not in rebuilt
    assert len(rebuilt) == 4
    assert [
        entry.scheduled_at for entry in rebuilt if entry.status is ScheduleStatus.PENDING
    ] == proposed[2:]


def test_val_schedule_009_rebuild_rejects_count_below_processed_total() -> None:
    """VAL-SCHEDULE-009: too-small count leaves the input objects untouched."""

    existing = [
        item(1, 1, 2, status=ScheduleStatus.COMPLETED),
        item(2, 2, 3, status=ScheduleStatus.SKIPPED),
    ]

    with pytest.raises(ScheduleValidationError, match="不可少於"):
        rebuild_pending_schedules(
            existing,
            [datetime(2026, 2, 2, 9, 30)],
            task_id=7,
            start_at=datetime(2026, 2, 1, 9, 30),
        )

    assert existing[0].status is ScheduleStatus.COMPLETED
    assert existing[1].status is ScheduleStatus.SKIPPED
