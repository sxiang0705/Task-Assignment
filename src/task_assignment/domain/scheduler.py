"""Forgetting-curve and manual schedule generation rules."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from task_assignment.domain.errors import ScheduleValidationError

FORGETTING_CURVE_DAYS = (1, 3, 7, 14, 30, 60, 90, 120, 180, 365)
MIN_CURVE_REVIEWS = 3
MAX_CURVE_REVIEWS = len(FORGETTING_CURVE_DAYS)


def generate_curve_schedule(start_at: datetime, review_count: int) -> tuple[datetime, ...]:
    """Generate 3-10 naive local wall-clock review times."""

    _require_local_wall_time(start_at, "開始時間")
    if isinstance(review_count, bool) or not MIN_CURVE_REVIEWS <= review_count <= MAX_CURVE_REVIEWS:
        raise ScheduleValidationError("遺忘曲線複習次數必須介於 3 到 10 次。")
    return tuple(
        start_at + timedelta(hours=24 * interval)
        for interval in FORGETTING_CURVE_DAYS[:review_count]
    )


def validate_manual_schedule(
    start_at: datetime, review_times: Iterable[datetime]
) -> tuple[datetime, ...]:
    """Validate and chronologically normalize user-supplied review times."""

    _require_local_wall_time(start_at, "開始時間")
    proposed = tuple(review_times)
    for review_time in proposed:
        _require_local_wall_time(review_time, "複習時間")
        if review_time < start_at:
            raise ScheduleValidationError("複習時間不可早於任務開始時間。")

    if len(set(proposed)) != len(proposed):
        raise ScheduleValidationError("複習時間不可重複。")
    return tuple(sorted(proposed))


def _require_local_wall_time(value: datetime, label: str) -> None:
    if not isinstance(value, datetime):
        raise ScheduleValidationError(f"{label}必須是日期時間。")
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise ScheduleValidationError(f"{label}必須使用不含時區位移的本機牆上時間。")
