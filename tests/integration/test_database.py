from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest

from task_assignment.domain.enums import ScheduleMode, ScheduleStatus, TaskStatus
from task_assignment.domain.models import Task
from task_assignment.domain.scheduler import generate_curve_schedule
from task_assignment.infrastructure.database.connection import Database
from task_assignment.infrastructure.database.migrations import (
    MIGRATIONS,
    DatabaseMigrationError,
    Migration,
    apply_migrations,
    current_schema_version,
)
from task_assignment.infrastructure.database.repositories import (
    CatalogRepository,
    RepositoryError,
    ReviewRepository,
    TaskRepository,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    result = Database(tmp_path / "task_assignment.db")
    result.initialize()
    return result


def curve_task(name: str = "資料庫測試") -> tuple[Task, tuple[datetime, ...]]:
    start = datetime(2026, 9, 1, 9, 30)
    return (
        Task(
            name=name,
            description="Repository integration",
            start_at=start,
            schedule_mode=ScheduleMode.CURVE,
            review_count=3,
        ),
        generate_curve_schedule(start, 3),
    )


def test_val_db_001_every_connection_enables_foreign_keys_and_wal(database: Database) -> None:
    """VAL-DB-001: connection-local FK is enabled by the single factory."""

    with database.connection() as first:
        first_foreign_keys = first.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = first.execute("PRAGMA journal_mode").fetchone()[0]
    with database.connection() as second:
        second_foreign_keys = second.execute("PRAGMA foreign_keys").fetchone()[0]

    assert first_foreign_keys == 1
    assert second_foreign_keys == 1
    assert journal_mode == "wal"


def test_val_db_002_empty_database_migrates_to_current_schema(database: Database) -> None:
    """VAL-DB-002: migration creates all required tables from an empty file."""

    required_tables = {
        "schema_migrations",
        "tasks",
        "review_schedules",
        "review_records",
        "categories",
        "tags",
        "task_tags",
        "settings",
        "assets",
        "backup_logs",
    }
    with database.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = current_schema_version(connection)

    assert required_tables.issubset(tables)
    assert version == MIGRATIONS[-1].version


def test_val_db_003_failed_migration_rolls_back_schema_and_version(database: Database) -> None:
    """VAL-DB-003: no partial table or version survives a failed migration."""

    broken = Migration(
        version=2,
        name="broken_test_migration",
        statements=(
            "CREATE TABLE must_rollback(id INTEGER PRIMARY KEY)",
            "THIS IS NOT VALID SQL",
        ),
    )
    with database.connection() as connection:
        with pytest.raises(DatabaseMigrationError):
            apply_migrations(connection, (*MIGRATIONS, broken))
        partial_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("must_rollback",),
        ).fetchone()
        version = current_schema_version(connection)

    assert partial_table is None
    assert version == 1


def test_val_db_004_task_and_schedules_roll_back_together(database: Database) -> None:
    """VAL-DB-004: a late FK failure removes the earlier task and schedules."""

    repository = TaskRepository(database)
    task, times = curve_task()

    with pytest.raises(RepositoryError):
        repository.create(task, times, tag_ids=[999], now=datetime(2026, 9, 1, 10))

    with database.connection() as connection:
        task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        schedule_count = connection.execute("SELECT COUNT(*) FROM review_schedules").fetchone()[0]
    assert task_count == 0
    assert schedule_count == 0


def test_val_db_005_review_cross_table_failure_is_atomic(database: Database) -> None:
    """VAL-DB-005: failed history insert rolls the schedule transition back."""

    task_repository = TaskRepository(database)
    review_repository = ReviewRepository(database)
    task, times = curve_task()
    created = task_repository.create(task, times, now=datetime(2026, 9, 1, 10))
    schedule = review_repository.list_for_task(created.id)[0]
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_review_history
            BEFORE INSERT ON review_records
            BEGIN
                SELECT RAISE(ABORT, 'injected history failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        review_repository.complete(
            schedule.id,
            occurred_at=datetime(2026, 9, 2, 10),
            today=date(2026, 9, 2),
        )

    unchanged = review_repository.list_for_task(created.id)[0]
    assert unchanged.status is ScheduleStatus.PENDING
    assert review_repository.records_for_task(created.id) == ()


def test_val_db_006_delete_task_cascades_schedules_history_and_tags(database: Database) -> None:
    """VAL-DB-006: task deletion leaves no dependent rows."""

    catalog = CatalogRepository(database)
    task_repository = TaskRepository(database)
    review_repository = ReviewRepository(database)
    tag_id = catalog.create_tag("重要", now=datetime(2026, 9, 1, 8))
    task, times = curve_task()
    created = task_repository.create(
        task,
        times,
        tag_ids=[tag_id],
        now=datetime(2026, 9, 1, 10),
    )
    first_schedule = review_repository.list_for_task(created.id)[0]
    review_repository.complete(
        first_schedule.id,
        occurred_at=datetime(2026, 9, 2, 10),
        today=date(2026, 9, 2),
    )

    task_repository.delete(created.id)

    with database.connection() as connection:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("tasks", "review_schedules", "review_records", "task_tags")
        }
        tag_count = connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    assert counts == {table: 0 for table in counts}
    assert tag_count == 1


def test_val_db_007_integrity_check_passes(database: Database) -> None:
    """VAL-DB-007: a normal database reports an exact integrity result of ok."""

    database.integrity_check()


def test_val_db_008_search_input_cannot_change_sql_structure(database: Database) -> None:
    """VAL-DB-008/VAL-SECURITY-001: search remains parameterized."""

    repository = TaskRepository(database)
    task, times = curve_task("正常任務")
    repository.create(task, times, now=datetime(2026, 9, 1, 10))

    result = repository.search("%' OR 1=1 --")

    assert result == ()
    assert len(repository.list_all()) == 1


def test_val_db_009_required_query_indexes_exist(database: Database) -> None:
    """VAL-DB-009: status, date, and relationship indexes are installed."""

    expected = {
        "idx_tasks_status",
        "idx_tasks_category",
        "idx_review_schedules_due",
        "idx_review_schedules_task_status",
        "idx_review_records_task",
        "idx_task_tags_tag",
    }
    with database.connection() as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }

    assert expected.issubset(indexes)


def test_val_db_010_task_state_pause_and_timestamps_round_trip(database: Database) -> None:
    """VAL-DB-010: task state, pause date, and timestamps round-trip."""

    repository = TaskRepository(database)
    task, times = curve_task()
    created_at = datetime(2026, 9, 1, 10, 5, 7)
    created = repository.create(task, times, now=created_at)
    paused = repository.set_paused(
        created.id,
        paused_on=date(2026, 9, 3),
        now=datetime(2026, 9, 3, 11),
    )

    assert paused.status is TaskStatus.PAUSED
    assert paused.is_paused
    assert paused.paused_on == date(2026, 9, 3)
    assert paused.created_at == created_at
    assert paused.updated_at == datetime(2026, 9, 3, 11)
