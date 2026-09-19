"""Ordered, atomic SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime


class DatabaseMigrationError(RuntimeError):
    """Raised when a migration cannot be applied as one atomic change."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="initial_schema",
        statements=(
            """
            CREATE TABLE categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                description TEXT NOT NULL DEFAULT '',
                category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                start_at TEXT NOT NULL,
                schedule_mode TEXT NOT NULL CHECK (schedule_mode IN ('curve', 'manual')),
                review_count INTEGER,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'not_started', 'in_progress', 'due_today', 'overdue',
                        'completed', 'incomplete', 'paused', 'archived'
                    )
                ),
                completion_rate REAL NOT NULL DEFAULT 0
                    CHECK (completion_rate >= 0 AND completion_rate <= 100),
                is_paused INTEGER NOT NULL DEFAULT 0 CHECK (is_paused IN (0, 1)),
                paused_on TEXT,
                is_archived INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (schedule_mode = 'curve' AND review_count BETWEEN 3 AND 10)
                    OR (schedule_mode = 'manual' AND review_count IS NULL)
                ),
                CHECK (
                    (is_paused = 1 AND paused_on IS NOT NULL)
                    OR (is_paused = 0 AND paused_on IS NULL)
                )
            )
            """,
            """
            CREATE TABLE review_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'completed', 'skipped')),
                processed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (task_id, sequence),
                UNIQUE (task_id, scheduled_at),
                CHECK (
                    (status = 'pending' AND processed_at IS NULL)
                    OR (status IN ('completed', 'skipped') AND processed_at IS NOT NULL)
                )
            )
            """,
            """
            CREATE TABLE review_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                schedule_id INTEGER,
                action TEXT NOT NULL
                    CHECK (action IN ('completed', 'skipped', 'postponed', 'merged')),
                occurred_at TEXT NOT NULL,
                previous_scheduled_at TEXT,
                resulting_scheduled_at TEXT,
                merged_into_schedule_id INTEGER
            )
            """,
            """
            CREATE TABLE task_tags (
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (task_id, tag_id)
            )
            """,
            """
            CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK (kind IN ('background', 'sticker')),
                relative_path TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                scope_page TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE backup_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation TEXT NOT NULL CHECK (operation IN ('backup', 'import', 'gmail_send')),
                occurred_at TEXT NOT NULL,
                recipient TEXT,
                backup_filename TEXT,
                backup_checksum TEXT,
                success INTEGER NOT NULL CHECK (success IN (0, 1)),
                error_message TEXT,
                gmail_message_id TEXT
            )
            """,
            "CREATE INDEX idx_tasks_status ON tasks(status)",
            "CREATE INDEX idx_tasks_category ON tasks(category_id)",
            "CREATE INDEX idx_tasks_start_at ON tasks(start_at)",
            "CREATE INDEX idx_tasks_created_at ON tasks(created_at)",
            "CREATE INDEX idx_review_schedules_due ON review_schedules(status, scheduled_at)",
            "CREATE INDEX idx_review_schedules_task_status ON review_schedules(task_id, status)",
            "CREATE INDEX idx_review_records_task ON review_records(task_id, occurred_at)",
            "CREATE INDEX idx_task_tags_tag ON task_tags(tag_id, task_id)",
            "CREATE INDEX idx_assets_kind ON assets(kind, enabled)",
            "CREATE INDEX idx_backup_logs_time ON backup_logs(occurred_at)",
        ),
    ),
)


def apply_migrations(
    connection: sqlite3.Connection,
    migrations: Iterable[Migration] = MIGRATIONS,
    *,
    now_factory: Callable[[], datetime] = datetime.now,
) -> None:
    """Apply each unapplied migration in its own explicit transaction."""

    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    _ensure_migration_ledger(connection)
    applied = {int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations")}
    known = {migration.version for migration in ordered}
    unknown = applied.difference(known)
    if unknown:
        versions = ", ".join(str(value) for value in sorted(unknown))
        raise DatabaseMigrationError(f"資料庫 schema 版本比程式新：{versions}")

    for migration in ordered:
        if migration.version in applied:
            continue
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (migration.version, now_factory().isoformat(timespec="seconds")),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise DatabaseMigrationError(
                f"資料庫 migration {migration.version} ({migration.name}) 失敗。"
            ) from exc


def current_schema_version(connection: sqlite3.Connection) -> int:
    _ensure_migration_ledger(connection)
    row = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def _ensure_migration_ledger(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise DatabaseMigrationError("無法建立 schema migration 紀錄。") from exc
