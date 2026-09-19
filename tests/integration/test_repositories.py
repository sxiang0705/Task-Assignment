from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest

from task_assignment.domain.enums import ReviewAction, ScheduleMode, ScheduleStatus, TaskStatus
from task_assignment.domain.errors import ReviewTransitionError, ScheduleValidationError
from task_assignment.domain.models import Task
from task_assignment.domain.scheduler import generate_curve_schedule
from task_assignment.infrastructure.database.connection import Database
from task_assignment.infrastructure.database.repositories import (
    AssetEntry,
    AssetRepository,
    BackupLogEntry,
    BackupLogRepository,
    CatalogRepository,
    RepositoryError,
    ReviewRepository,
    SettingsRepository,
    TaskRepository,
)


@pytest.fixture
def repositories(tmp_path: Path) -> tuple[Database, TaskRepository, ReviewRepository]:
    database = Database(tmp_path / "task_assignment.db")
    database.initialize()
    return database, TaskRepository(database), ReviewRepository(database)


def create_curve_task(
    tasks: TaskRepository,
    *,
    name: str = "間隔複習",
    start: datetime = datetime(2026, 1, 1, 9, 30),
) -> Task:
    return tasks.create(
        Task(
            name=name,
            start_at=start,
            schedule_mode=ScheduleMode.CURVE,
            review_count=3,
        ),
        generate_curve_schedule(start, 3),
        now=start,
    )


def test_val_review_002_003_complete_updates_history_rate_and_task_status(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-REVIEW-002/003: completion persists atomically through final status."""

    _, tasks, reviews = repositories
    created = create_curve_task(tasks)

    for schedule in reviews.list_for_task(created.id):
        reviews.complete(
            schedule.id,
            occurred_at=schedule.scheduled_at,
            today=schedule.scheduled_at.date(),
        )

    stored = tasks.get(created.id)
    assert stored.status is TaskStatus.COMPLETED
    assert stored.completion_rate == 100.0
    assert len(reviews.records_for_task(created.id)) == 3


def test_val_review_004_005_006_skip_is_irreversible_and_incomplete(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-REVIEW-004/005/006: skip stays in denominator and ends incomplete."""

    _, tasks, reviews = repositories
    created = create_curve_task(tasks)
    schedules = reviews.list_for_task(created.id)
    reviews.complete(
        schedules[0].id,
        occurred_at=schedules[0].scheduled_at,
        today=schedules[0].scheduled_at.date(),
    )
    skipped = reviews.skip(
        schedules[1].id,
        occurred_at=schedules[1].scheduled_at,
        today=schedules[1].scheduled_at.date(),
    )
    reviews.complete(
        schedules[2].id,
        occurred_at=schedules[2].scheduled_at,
        today=schedules[2].scheduled_at.date(),
    )

    stored = tasks.get(created.id)
    assert stored.status is TaskStatus.INCOMPLETE
    assert stored.completion_rate == pytest.approx(200 / 3)
    with pytest.raises(ReviewTransitionError):
        reviews.complete(
            skipped.id,
            occurred_at=datetime(2026, 1, 10),
            today=date(2026, 1, 10),
        )


def test_val_review_007_008_postpone_persists_shift_and_excludes_terminal_items(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-REVIEW-007/008: repository shifts current/future pending only."""

    _, tasks, reviews = repositories
    created = create_curve_task(tasks)
    schedules = reviews.list_for_task(created.id)
    reviews.complete(
        schedules[0].id,
        occurred_at=schedules[0].scheduled_at,
        today=schedules[0].scheduled_at.date(),
    )

    updated = reviews.postpone(
        schedules[1].id,
        occurred_at=datetime(2026, 1, 4, 10),
        today=date(2026, 1, 4),
    )
    by_id = {item.id: item for item in updated}

    assert by_id[schedules[0].id].scheduled_at == schedules[0].scheduled_at
    assert by_id[schedules[1].id].scheduled_at == datetime(2026, 1, 5, 9, 30)
    assert by_id[schedules[2].id].scheduled_at == datetime(2026, 1, 9, 9, 30)
    actions = [record.action for record in reviews.records_for_task(created.id)]
    assert actions.count(ReviewAction.POSTPONED) == 2


def test_val_review_009_postpone_collision_deletes_one_schedule_and_logs_merge(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-REVIEW-009: persisted collision reduces rows and records the merge."""

    database, tasks, reviews = repositories
    start = datetime(2026, 1, 1, 9, 30)
    created = tasks.create(
        Task(
            name="合併測試",
            start_at=start,
            schedule_mode=ScheduleMode.MANUAL,
            review_count=None,
        ),
        [datetime(2026, 1, day, 9, 30) for day in (2, 3, 4)],
        now=start,
    )
    schedules = reviews.list_for_task(created.id)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE review_schedules SET scheduled_at = ? WHERE id = ?",
            ("2026-01-10T09:30:00.000000", schedules[0].id),
        )
        connection.execute(
            "UPDATE review_schedules SET scheduled_at = ? WHERE id = ?",
            ("2026-01-01T09:30:00.000000", schedules[2].id),
        )
        connection.execute(
            "UPDATE review_schedules SET scheduled_at = ? WHERE id = ?",
            ("2026-01-04T09:30:00.000000", schedules[0].id),
        )

    result = reviews.postpone(
        schedules[1].id,
        occurred_at=datetime(2026, 1, 3, 10),
        today=date(2026, 1, 3),
    )

    assert len(result) == 2
    assert len(reviews.list_for_task(created.id)) == 2
    actions = [record.action for record in reviews.records_for_task(created.id)]
    assert ReviewAction.MERGED in actions


def test_val_review_007_postpone_consecutive_dates_avoids_transient_collision(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-REVIEW-007: adjacent dates shift atomically without a transient conflict."""

    _, tasks, reviews = repositories
    start = datetime(2026, 1, 1, 9, 30)
    created = tasks.create(
        Task(
            name="連續日期推延",
            start_at=start,
            schedule_mode=ScheduleMode.MANUAL,
            review_count=None,
        ),
        [datetime(2026, 1, day, 9, 30) for day in (2, 3, 4)],
        now=start,
    )
    schedules = reviews.list_for_task(created.id)

    reviews.postpone(
        schedules[0].id,
        occurred_at=datetime(2026, 1, 2, 10),
        today=date(2026, 1, 2),
    )

    assert [item.scheduled_at.day for item in reviews.list_for_task(created.id)] == [3, 4, 5]


def test_val_task_008_009_pause_excludes_due_and_resume_shifts_pending(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-TASK-008/009: paused tasks disappear and resume shifts pending dates."""

    _, tasks, reviews = repositories
    created = create_curve_task(tasks)
    original = reviews.list_for_task(created.id)
    tasks.set_paused(created.id, paused_on=date(2026, 1, 2), now=datetime(2026, 1, 2, 10))

    assert reviews.list_due_today(date(2026, 1, 2)) == ()
    resumed = tasks.resume(
        created.id,
        resumed_on=date(2026, 1, 5),
        now=datetime(2026, 1, 5, 10),
    )
    shifted = reviews.list_for_task(created.id)

    assert not resumed.is_paused
    assert resumed.paused_on is None
    assert [item.scheduled_at for item in shifted] == [
        item.scheduled_at.replace(day=item.scheduled_at.day + 3) for item in original
    ]


def test_val_task_009_resume_consecutive_dates_avoids_transient_collision(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-TASK-009: adjacent pending dates resume without a transient conflict."""

    _, tasks, reviews = repositories
    start = datetime(2026, 1, 1, 9, 30)
    created = tasks.create(
        Task(
            name="連續日期恢復",
            start_at=start,
            schedule_mode=ScheduleMode.MANUAL,
            review_count=None,
        ),
        [datetime(2026, 1, day, 9, 30) for day in (2, 3, 4)],
        now=start,
    )
    tasks.set_paused(created.id, paused_on=date(2026, 1, 2), now=datetime(2026, 1, 2, 10))

    tasks.resume(created.id, resumed_on=date(2026, 1, 3), now=datetime(2026, 1, 3, 10))

    assert [item.scheduled_at.day for item in reviews.list_for_task(created.id)] == [3, 4, 5]


def test_val_schedule_008_009_rebuild_is_atomic_and_preserves_history(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-SCHEDULE-008/009: persisted rebuild preserves terminal rows."""

    _, tasks, reviews = repositories
    created = create_curve_task(tasks)
    original = reviews.list_for_task(created.id)
    completed = reviews.complete(
        original[0].id,
        occurred_at=original[0].scheduled_at,
        today=original[0].scheduled_at.date(),
    )
    proposed = [datetime(2026, 2, day, 9, 30) for day in (2, 3, 7, 14)]

    rebuilt = reviews.rebuild_pending(
        created.id,
        proposed,
        start_at=datetime(2026, 2, 1, 9, 30),
        schedule_mode=ScheduleMode.MANUAL,
        review_count=None,
        today=date(2026, 2, 1),
        now=datetime(2026, 2, 1, 10),
    )

    assert any(
        item.id == completed.id and item.status is ScheduleStatus.COMPLETED for item in rebuilt
    )
    assert [
        item.scheduled_at for item in rebuilt if item.status is ScheduleStatus.PENDING
    ] == proposed[1:]
    before_failed_change = reviews.list_for_task(created.id)
    with pytest.raises(ScheduleValidationError):
        reviews.rebuild_pending(
            created.id,
            [],
            start_at=datetime(2026, 3, 1, 9, 30),
            schedule_mode=ScheduleMode.MANUAL,
            review_count=None,
            today=date(2026, 3, 1),
        )
    assert reviews.list_for_task(created.id) == before_failed_change


def test_val_task_002_004_task_update_archive_restore_and_catalog_search(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-TASK-002/004: CRUD fields persist and archive is reversible."""

    database, tasks, _ = repositories
    catalog = CatalogRepository(database)
    category_id = catalog.create_category("語言", now=datetime(2026, 1, 1, 8))
    tag_id = catalog.create_tag("英文", now=datetime(2026, 1, 1, 8))
    start = datetime(2026, 1, 1, 9, 30)
    created = tasks.create(
        Task(
            name="原始名稱",
            description="原始說明",
            category_id=category_id,
            start_at=start,
            schedule_mode=ScheduleMode.CURVE,
            review_count=3,
        ),
        generate_curve_schedule(start, 3),
        tag_ids=[tag_id],
        now=start,
    )
    updated = tasks.update(
        replace(created, name="英文單字", description="每天複習"),
        now=datetime(2026, 1, 1, 10),
    )

    assert updated.name == "英文單字"
    assert updated.description == "每天複習"
    assert updated.category_id == category_id
    assert [task.id for task in tasks.search("英文")] == [created.id]

    archived = tasks.set_archived(
        created.id,
        archived=True,
        now=datetime(2026, 1, 1, 11),
    )
    restored = tasks.set_archived(
        created.id,
        archived=False,
        now=datetime(2026, 1, 1, 12),
    )

    assert archived.status is TaskStatus.ARCHIVED
    assert archived.is_archived
    assert restored.status is TaskStatus.IN_PROGRESS
    assert not restored.is_archived


def test_val_date_002_003_due_and_overdue_queries_use_date_boundaries(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-DATE-002/003: due queries ignore today's time-of-day."""

    _, tasks, reviews = repositories
    created = create_curve_task(tasks)
    first = reviews.list_for_task(created.id)[0]

    assert [item.id for item in reviews.list_due_today(date(2026, 1, 2))] == [first.id]
    assert reviews.list_overdue(date(2026, 1, 2)) == ()
    assert [item.id for item in reviews.list_overdue(date(2026, 1, 3))] == [first.id]


def test_settings_assets_and_backup_logs_use_separate_tables(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """M2: settings, asset metadata, and backup logs round-trip separately."""

    database, _, _ = repositories
    settings = SettingsRepository(database)
    assets = AssetRepository(database)
    logs = BackupLogRepository(database)
    timestamp = datetime(2026, 1, 1, 10)

    settings.set("appearance", {"theme": "green", "sticker": True}, now=timestamp)
    asset = assets.add(
        AssetEntry(kind="background", relative_path="assets\\backgrounds\\study.png"),
        now=timestamp,
    )
    log_id = logs.add(
        BackupLogEntry(
            operation="backup",
            occurred_at=timestamp,
            success=True,
            backup_filename="task-assignment-backup.zip",
            backup_checksum="abc123",
        )
    )

    assert settings.get("appearance") == {"theme": "green", "sticker": True}
    assert settings.get("missing", "fallback") == "fallback"
    assert asset.id is not None
    assert asset.relative_path == "assets/backgrounds/study.png"
    with database.connection() as connection:
        stored_log = connection.execute(
            "SELECT operation, success, backup_filename FROM backup_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
    assert tuple(stored_log) == ("backup", 1, "task-assignment-backup.zip")


def test_asset_repository_rejects_paths_outside_application_data(
    repositories: tuple[Database, TaskRepository, ReviewRepository],
) -> None:
    """VAL-SECURITY-002: asset metadata accepts only relative in-app paths."""

    database, _, _ = repositories
    assets = AssetRepository(database)

    with pytest.raises(RepositoryError, match="相對路徑"):
        assets.add(AssetEntry(kind="background", relative_path="../outside.png"))
