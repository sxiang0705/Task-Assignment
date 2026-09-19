"""SQLite connection lifecycle, transactions, and integrity checks."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from task_assignment.infrastructure.database.migrations import apply_migrations


class DatabaseError(RuntimeError):
    """Base error for database initialization and access failures."""


class DatabaseOpenError(DatabaseError):
    """Raised when an existing database cannot be opened safely."""


class DatabaseIntegrityError(DatabaseError):
    """Raised when SQLite reports database corruption or inconsistency."""


def connect_database(path: Path) -> sqlite3.Connection:
    """Open one configured SQLite connection.

    Foreign keys are connection-local in SQLite, so this function is the only
    supported connection entry point for repositories.
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise DatabaseOpenError(f"無法開啟資料庫：{path}") from exc


class Database:
    """Owns a database path while keeping individual connections short-lived."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            apply_migrations(connection)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect_database(self.path)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write operation atomically and always release the file handle."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def integrity_check(self) -> None:
        with self.connection() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            detail = "no result" if result is None else str(result[0])
            raise DatabaseIntegrityError(f"資料庫完整性檢查失敗：{detail}")

    def backup_to(self, destination: str | Path) -> None:
        """Create a transactionally consistent snapshot with SQLite's backup API."""

        target_path = Path(destination)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target: sqlite3.Connection | None = None
        try:
            with self.connection() as source:
                target = sqlite3.connect(target_path)
                source.backup(target)
                target.commit()
        except sqlite3.Error as exc:
            raise DatabaseError("無法建立一致性的 SQLite 備份快照。") from exc
        finally:
            if target is not None:
                target.close()

    def checkpoint(self) -> None:
        """Flush WAL content before replacing the database file during import."""

        try:
            with self.connection() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            raise DatabaseError("無法準備取代目前資料庫。") from exc


def initialize_database(path: Path) -> None:
    """Compatibility entry point used by the application bootstrap."""

    Database(path).initialize()
