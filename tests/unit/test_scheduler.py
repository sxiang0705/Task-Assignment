from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from task_assignment.domain.errors import ScheduleValidationError
from task_assignment.domain.scheduler import (
    FORGETTING_CURVE_DAYS,
    generate_curve_schedule,
    validate_manual_schedule,
)


def test_val_schedule_001_curve_uses_documented_intervals() -> None:
    """VAL-SCHEDULE-001: all ten curve intervals are fixed and ordered."""

    start = datetime(2026, 1, 30, 21, 45)

    generated = generate_curve_schedule(start, 10)

    assert generated == tuple(start + timedelta(days=value) for value in FORGETTING_CURVE_DAYS)


def test_val_schedule_002_first_review_is_exactly_24_hours_later() -> None:
    """VAL-SCHEDULE-002: day one is start + 24 hours."""

    start = datetime(2026, 12, 31, 23, 15)

    generated = generate_curve_schedule(start, 3)

    assert generated[0] - start == timedelta(hours=24)


def test_val_schedule_003_curve_preserves_wall_clock_time() -> None:
    """VAL-SCHEDULE-003: every generated review preserves hour/minute."""

    generated = generate_curve_schedule(datetime(2026, 3, 1, 7, 42, 19), 10)

    assert {(value.hour, value.minute, value.second) for value in generated} == {(7, 42, 19)}


@pytest.mark.parametrize("count", [0, 2, 11, True])
def test_val_schedule_004_curve_rejects_counts_outside_three_to_ten(count: int) -> None:
    """VAL-SCHEDULE-004: curve count is an integer from 3 through 10."""

    with pytest.raises(ScheduleValidationError):
        generate_curve_schedule(datetime(2026, 1, 1, 10), count)


def test_val_schedule_005_manual_rejects_duplicates_and_times_before_start() -> None:
    """VAL-SCHEDULE-005: manual times are unique and not before start."""

    start = datetime(2026, 1, 1, 10)
    with pytest.raises(ScheduleValidationError, match="重複"):
        validate_manual_schedule(start, [start + timedelta(days=1)] * 2)
    with pytest.raises(ScheduleValidationError, match="早於"):
        validate_manual_schedule(start, [start - timedelta(minutes=1)])


def test_val_schedule_006_manual_times_are_sorted() -> None:
    """VAL-SCHEDULE-006: input order does not affect normalized order."""

    start = datetime(2026, 1, 1, 10)
    later = start + timedelta(days=3)
    earlier = start + timedelta(days=1)

    assert validate_manual_schedule(start, [later, earlier]) == (earlier, later)


def test_local_wall_times_reject_timezone_aware_values() -> None:
    with pytest.raises(ScheduleValidationError, match="時區"):
        generate_curve_schedule(datetime(2026, 1, 1, tzinfo=UTC), 3)
