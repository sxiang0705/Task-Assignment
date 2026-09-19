"""Pure review state transitions, postponement, and rebuild behavior."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta

from task_assignment.domain.enums import ReviewAction, ScheduleStatus
from task_assignment.domain.errors import ReviewTransitionError, ScheduleValidationError
from task_assignment.domain.models import (
    PostponeResult,
    ReviewRecord,
    ReviewSchedule,
    ReviewTransition,
)
from task_assignment.domain.scheduler import validate_manual_schedule


def complete_review(schedule: ReviewSchedule, occurred_at: datetime) -> ReviewTransition:
    return _finish_review(schedule, occurred_at, ScheduleStatus.COMPLETED, ReviewAction.COMPLETED)


def skip_review(schedule: ReviewSchedule, occurred_at: datetime) -> ReviewTransition:
    return _finish_review(schedule, occurred_at, ScheduleStatus.SKIPPED, ReviewAction.SKIPPED)


def _finish_review(
    schedule: ReviewSchedule,
    occurred_at: datetime,
    resulting_status: ScheduleStatus,
    action: ReviewAction,
) -> ReviewTransition:
    if schedule.status is not ScheduleStatus.PENDING:
        raise ReviewTransitionError("已完成或已略過的複習不可再次修改。")
    updated = replace(schedule, status=resulting_status, processed_at=occurred_at)
    return ReviewTransition(
        schedule=updated,
        record=ReviewRecord(
            task_id=schedule.task_id,
            schedule_id=schedule.id,
            action=action,
            occurred_at=occurred_at,
            previous_scheduled_at=schedule.scheduled_at,
            resulting_scheduled_at=schedule.scheduled_at,
        ),
    )


def postpone_to_tomorrow(
    schedules: Iterable[ReviewSchedule],
    *,
    current_schedule_id: int,
    occurred_at: datetime,
) -> PostponeResult:
    """Shift the current and later pending sequence entries by one calendar day."""

    items = tuple(schedules)
    selected = next((item for item in items if item.id == current_schedule_id), None)
    if selected is None:
        raise ReviewTransitionError("找不到要推延的複習項目。")
    if selected.status is not ScheduleStatus.PENDING:
        raise ReviewTransitionError("已完成或已略過的複習不可推延。")

    shifted: list[ReviewSchedule] = []
    records: list[ReviewRecord] = []
    for item in items:
        should_shift = (
            item.task_id == selected.task_id
            and item.status is ScheduleStatus.PENDING
            and item.sequence >= selected.sequence
        )
        if not should_shift:
            shifted.append(item)
            continue

        resulting_time = item.scheduled_at + timedelta(days=1)
        shifted.append(replace(item, scheduled_at=resulting_time))
        records.append(
            ReviewRecord(
                task_id=item.task_id,
                schedule_id=item.id,
                action=ReviewAction.POSTPONED,
                occurred_at=occurred_at,
                previous_scheduled_at=item.scheduled_at,
                resulting_scheduled_at=resulting_time,
            )
        )

    merged, merge_records, removed_ids = _merge_pending_duplicates(shifted, occurred_at)
    return PostponeResult(
        schedules=tuple(merged),
        records=tuple((*records, *merge_records)),
        removed_schedule_ids=tuple(removed_ids),
    )


def resume_after_pause(
    schedules: Iterable[ReviewSchedule], *, paused_on: date, resumed_on: date
) -> tuple[ReviewSchedule, ...]:
    """Shift pending reviews by the number of paused local calendar days."""

    paused_days = (resumed_on - paused_on).days
    if paused_days < 0:
        raise ScheduleValidationError("恢復日期不可早於暫停日期。")
    return tuple(
        replace(item, scheduled_at=item.scheduled_at + timedelta(days=paused_days))
        if item.status is ScheduleStatus.PENDING
        else item
        for item in schedules
    )


def rebuild_pending_schedules(
    existing_schedules: Iterable[ReviewSchedule],
    proposed_times: Iterable[datetime],
    *,
    task_id: int,
    start_at: datetime,
) -> tuple[ReviewSchedule, ...]:
    """Preserve terminal history and replace only pending schedule entries."""

    existing = tuple(existing_schedules)
    if any(item.task_id != task_id for item in existing):
        raise ScheduleValidationError("排程包含其他任務的資料。")
    normalized_times = validate_manual_schedule(start_at, proposed_times)
    processed = tuple(item for item in existing if item.status is not ScheduleStatus.PENDING)
    if len(normalized_times) < len(processed):
        raise ScheduleValidationError("新的複習次數不可少於已完成與已略過次數總和。")

    replacement_times = normalized_times[len(processed) :]
    historical_times = {item.scheduled_at for item in processed}
    if historical_times.intersection(replacement_times):
        raise ScheduleValidationError("新的未處理排程不可與歷史複習時間重複。")

    used_sequences = {item.sequence for item in processed}
    available_sequences = (
        value for value in range(1, len(normalized_times) + 1) if value not in used_sequences
    )
    replacements = tuple(
        ReviewSchedule(
            id=None,
            task_id=task_id,
            sequence=next(available_sequences),
            scheduled_at=scheduled_at,
        )
        for scheduled_at in replacement_times
    )
    return tuple(sorted((*processed, *replacements), key=lambda item: item.sequence))


def _merge_pending_duplicates(
    schedules: Iterable[ReviewSchedule], occurred_at: datetime
) -> tuple[list[ReviewSchedule], list[ReviewRecord], list[int]]:
    retained: list[ReviewSchedule] = []
    records: list[ReviewRecord] = []
    removed_ids: list[int] = []
    pending_by_key: dict[tuple[int, datetime], ReviewSchedule] = {}

    for item in sorted(schedules, key=lambda value: (value.task_id, value.sequence)):
        if item.status is not ScheduleStatus.PENDING:
            retained.append(item)
            continue

        key = (item.task_id, item.scheduled_at)
        target = pending_by_key.get(key)
        if target is None:
            pending_by_key[key] = item
            retained.append(item)
            continue

        if item.id is not None:
            removed_ids.append(item.id)
        records.append(
            ReviewRecord(
                task_id=item.task_id,
                schedule_id=item.id,
                action=ReviewAction.MERGED,
                occurred_at=occurred_at,
                previous_scheduled_at=item.scheduled_at,
                resulting_scheduled_at=target.scheduled_at,
                merged_into_schedule_id=target.id,
            )
        )

    return retained, records, removed_ids
