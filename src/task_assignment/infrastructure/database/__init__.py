"""SQLite connection and migration infrastructure."""

from task_assignment.infrastructure.database.connection import (
    Database,
    DatabaseError,
    DatabaseIntegrityError,
    DatabaseOpenError,
    connect_database,
    initialize_database,
)
from task_assignment.infrastructure.database.migrations import (
    MIGRATIONS,
    DatabaseMigrationError,
    Migration,
    current_schema_version,
)
from task_assignment.infrastructure.database.repositories import (
    AssetEntry,
    AssetRepository,
    BackupLogEntry,
    BackupLogRepository,
    CalendarMetrics,
    CatalogRepository,
    DashboardMetrics,
    EntityNotFoundError,
    NamedEntry,
    RepositoryError,
    ReviewRepository,
    SettingsRepository,
    TaskPageResult,
    TaskRepository,
)

__all__ = [
    "MIGRATIONS",
    "Database",
    "DatabaseError",
    "DatabaseIntegrityError",
    "DatabaseMigrationError",
    "DatabaseOpenError",
    "Migration",
    "AssetEntry",
    "AssetRepository",
    "BackupLogEntry",
    "BackupLogRepository",
    "CalendarMetrics",
    "CatalogRepository",
    "DashboardMetrics",
    "EntityNotFoundError",
    "NamedEntry",
    "RepositoryError",
    "ReviewRepository",
    "SettingsRepository",
    "TaskRepository",
    "TaskPageResult",
    "connect_database",
    "current_schema_version",
    "initialize_database",
]
