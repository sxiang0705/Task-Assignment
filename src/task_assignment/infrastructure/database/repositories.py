"""SQLite repositories for tasks, schedules, settings, assets, and logs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from task_assignment.domain.enums import (
    ReviewAction,
    ScheduleMode,
    ScheduleStatus,
    TaskStatus,
)
from task_assignment.domain.models import ReviewRecord, ReviewSchedule, Task
from task_assignment.domain.progress import completion_rate
from task_assignment.domain.review import (
    complete_review,
    postpone_to_tomorrow,
    rebuild_pending_schedules,
    resume_after_pause,
    skip_review,
)
from task_assignment.domain.scheduler import validate_manual_schedule
from task_assignment.domain.task_status import calculate_task_status
from task_assignment.infrastructure.database.connection import Database


class RepositoryError(RuntimeError):
    """Base class for repository failures safe for service translation."""


class EntityNotFoundError(RepositoryError):
    """Raised when an operation targets a missing database row."""


@dataclass(frozen=True, slots=True)
class AssetEntry:
    kind: str
    relative_path: str
    enabled: bool = True
    scope_page: str | None = None
    id: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BackupLogEntry:
    operation: str
    occurred_at: datetime
    success: bool
    recipient: str | None = None
    backup_filename: str | None = None
    backup_checksum: str | None = None
    error_message: str | None = None
    gmail_message_id: str | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class NamedEntry:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class TaskPageResult:
    tasks: tuple[Task, ...]
    total_count: int
    offset: int = 0


@dataclass(frozen=True, slots=True)
class DashboardMetrics:
    task_total: int
    schedule_total: int
    pending_count: int
    overdue_count: int
    due_today_count: int
    next_seven_days_count: int
    completed_count: int
    skipped_count: int


@dataclass(frozen=True, slots=True)
class CalendarMetrics:
    day: date
    schedule_count: int
    pending_count: int
    completed_count: int
    skipped_count: int
    overdue_count: int


class CatalogRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_category(self, name: str, *, now: datetime | None = None) -> int:
        return self._create_named("categories", name, now=now)

    def create_tag(self, name: str, *, now: datetime | None = None) -> int:
        return self._create_named("tags", name, now=now)

    def list_categories(self) -> tuple[NamedEntry, ...]:
        return self._list_named("categories")

    def list_tags(self) -> tuple[NamedEntry, ...]:
        return self._list_named("tags")

    def _list_named(self, table: str) -> tuple[NamedEntry, ...]:
        if table not in {"categories", "tags"}:
            raise RepositoryError("不支援的目錄資料表。")
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT id, name FROM {table} ORDER BY name COLLATE NOCASE, id"  # noqa: S608
            ).fetchall()
        return tuple(NamedEntry(id=int(row["id"]), name=str(row["name"])) for row in rows)

    def _create_named(self, table: str, name: str, *, now: datetime | None) -> int:
        if table not in {"categories", "tags"}:
            raise RepositoryError("不支援的目錄資料表。")
        normalized = name.strip()
        if not normalized:
            raise RepositoryError("名稱不可空白。")
        timestamp = _to_iso(now or datetime.now())
        with self.database.transaction() as connection:
            try:
                cursor = connection.execute(
                    f"INSERT INTO {table}(name, created_at) VALUES (?, ?)",  # noqa: S608
                    (normalized, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise RepositoryError(f"名稱已存在：{normalized}") from exc
        return int(cursor.lastrowid)


class TaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        task: Task,
        schedule_times: Iterable[datetime],
        *,
        tag_ids: Iterable[int] = (),
        now: datetime | None = None,
    ) -> Task:
        if task.id is not None:
            raise RepositoryError("新任務不可預先指定 ID。")
        name = task.name.strip()
        if not name:
            raise RepositoryError("任務名稱不可空白。")
        times = validate_manual_schedule(task.start_at, schedule_times)
        if task.schedule_mode is ScheduleMode.CURVE and len(times) != task.review_count:
            raise RepositoryError("遺忘曲線排程數量與複習次數不一致。")
        if task.schedule_mode is ScheduleMode.MANUAL and task.review_count is not None:
            raise RepositoryError("手動排程不可設定遺忘曲線複習次數。")

        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        unique_tag_ids = tuple(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO tasks(
                        name, description, category_id, start_at, schedule_mode,
                        review_count, status, completion_rate, is_paused, paused_on,
                        is_archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        task.description,
                        task.category_id,
                        _to_iso(task.start_at),
                        task.schedule_mode.value,
                        task.review_count,
                        TaskStatus.NOT_STARTED.value,
                        0.0,
                        int(task.is_paused),
                        task.paused_on.isoformat() if task.paused_on else None,
                        int(task.is_archived),
                        timestamp,
                        timestamp,
                    ),
                )
                task_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO review_schedules(
                        task_id, sequence, scheduled_at, status, processed_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        (
                            task_id,
                            sequence,
                            _to_iso(scheduled_at),
                            ScheduleStatus.PENDING.value,
                            timestamp,
                            timestamp,
                        )
                        for sequence, scheduled_at in enumerate(times, start=1)
                    ),
                )
                connection.executemany(
                    "INSERT INTO task_tags(task_id, tag_id) VALUES (?, ?)",
                    ((task_id, tag_id) for tag_id in unique_tag_ids),
                )
                _refresh_task_metrics(connection, task_id, timestamp_value.date(), timestamp)
        except sqlite3.Error as exc:
            raise RepositoryError("建立任務與排程失敗。") from exc
        return self.get(task_id)

    def get(self, task_id: int) -> Task:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError(f"找不到任務：{task_id}")
        return _task_from_row(row)

    def list_all(self) -> tuple[Task, ...]:
        with self.database.connection() as connection:
            rows = connection.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def search(self, query: str) -> tuple[Task, ...]:
        """Search task, description, category, and tag using bound parameters."""

        pattern = f"%{_escape_like(query.strip())}%"
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT t.*
                FROM tasks AS t
                LEFT JOIN categories AS c ON c.id = t.category_id
                LEFT JOIN task_tags AS tt ON tt.task_id = t.id
                LEFT JOIN tags AS tag ON tag.id = tt.tag_id
                WHERE t.name LIKE ? ESCAPE '\\'
                   OR t.description LIKE ? ESCAPE '\\'
                   OR c.name LIKE ? ESCAPE '\\'
                   OR tag.name LIKE ? ESCAPE '\\'
                ORDER BY t.name COLLATE NOCASE, t.id
                """,
                (pattern, pattern, pattern, pattern),
            ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def list_by_ids(self, task_ids: Iterable[int]) -> tuple[Task, ...]:
        ids = tuple(dict.fromkeys(int(task_id) for task_id in task_ids))
        if not ids:
            return ()
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE id IN ({placeholders}) ORDER BY id",  # noqa: S608
                ids,
            ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def query_page(
        self,
        *,
        text: str = "",
        statuses: Iterable[TaskStatus] = (),
        category_id: int | None = None,
        tag_id: int | None = None,
        scheduled_from: date | None = None,
        scheduled_to: date | None = None,
        include_archived: bool = False,
        sort_by: str = "created_at",
        descending: bool = False,
        limit: int = 100,
        offset: int = 0,
        focus_task_id: int | None = None,
    ) -> TaskPageResult:
        if limit < 1 or limit > 500 or offset < 0:
            raise RepositoryError("任務分頁參數不正確。")
        status_values = tuple(dict.fromkeys(status.value for status in statuses))
        predicates: list[str] = []
        parameters: list[object] = []
        normalized_text = text.strip()
        if normalized_text:
            pattern = f"%{_escape_like(normalized_text)}%"
            predicates.append(
                """
                (
                    t.name LIKE ? ESCAPE '\\'
                    OR t.description LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM categories AS c
                        WHERE c.id = t.category_id AND c.name LIKE ? ESCAPE '\\'
                    )
                    OR EXISTS (
                        SELECT 1
                        FROM task_tags AS tt_search
                        JOIN tags AS tag_search ON tag_search.id = tt_search.tag_id
                        WHERE tt_search.task_id = t.id
                          AND tag_search.name LIKE ? ESCAPE '\\'
                    )
                )
                """
            )
            parameters.extend((pattern, pattern, pattern, pattern))
        if not include_archived and TaskStatus.ARCHIVED.value not in status_values:
            predicates.append("t.is_archived = 0")
        if status_values:
            placeholders = ", ".join("?" for _ in status_values)
            predicates.append(f"t.status IN ({placeholders})")
            parameters.extend(status_values)
        if category_id is not None:
            predicates.append("t.category_id = ?")
            parameters.append(category_id)
        if tag_id is not None:
            predicates.append(
                "EXISTS (SELECT 1 FROM task_tags AS tt WHERE tt.task_id = t.id AND tt.tag_id = ?)"
            )
            parameters.append(tag_id)
        if scheduled_from is not None or scheduled_to is not None:
            schedule_predicates = ["s_filter.task_id = t.id"]
            if scheduled_from is not None:
                schedule_predicates.append("s_filter.scheduled_at >= ?")
                parameters.append(_to_iso(datetime.combine(scheduled_from, time.min)))
            if scheduled_to is not None:
                schedule_predicates.append("s_filter.scheduled_at < ?")
                parameters.append(
                    _to_iso(datetime.combine(scheduled_to + timedelta(days=1), time.min))
                )
            predicates.append(
                "EXISTS (SELECT 1 FROM review_schedules AS s_filter WHERE "
                + " AND ".join(schedule_predicates)
                + ")"
            )

        where_clause = " AND ".join(predicates) if predicates else "1 = 1"
        direction = "DESC" if descending else "ASC"
        if sort_by == "name":
            order_by = f"t.name COLLATE NOCASE {direction}, t.id {direction}"
        elif sort_by == "created_at":
            order_by = f"t.created_at {direction}, t.id {direction}"
        elif sort_by == "next_review":
            scheduled_direction = "DESC" if descending else "ASC"
            order_by = (
                "(next_review_sort IS NULL) ASC, "
                f"next_review_sort {scheduled_direction}, "
                f"t.id {scheduled_direction}"
            )
        else:
            raise RepositoryError("不支援的任務排序方式。")

        count_query = f"SELECT COUNT(*) FROM tasks AS t WHERE {where_clause}"  # noqa: S608
        eligible_query = f"""
            SELECT t.*,
                   (
                       SELECT MIN(s_sort.scheduled_at)
                       FROM review_schedules AS s_sort
                       WHERE s_sort.task_id = t.id AND s_sort.status = 'pending'
                   ) AS next_review_sort
            FROM tasks AS t
            WHERE {where_clause}
        """
        page_query = eligible_query + f" ORDER BY {order_by} LIMIT ? OFFSET ?"
        with self.database.connection() as connection:
            if focus_task_id is not None:
                rank_query = f"""
                    WITH eligible AS ({eligible_query}), ranked AS (
                        SELECT id, ROW_NUMBER() OVER (
                            ORDER BY {order_by.replace('t.', '')}
                        ) - 1 AS position FROM eligible
                    ) SELECT position FROM ranked WHERE id = ?
                """
                position = connection.execute(
                    rank_query, (*parameters, focus_task_id)
                ).fetchone()
                if position is None:
                    raise EntityNotFoundError("目標任務不存在或不符合目前篩選條件。")
                offset = int(position[0]) // limit * limit
            total_count = int(connection.execute(count_query, parameters).fetchone()[0])
            rows = connection.execute(
                page_query,
                (*parameters, limit, offset),
            ).fetchall()
        return TaskPageResult(
            tasks=tuple(_task_from_row(row) for row in rows),
            total_count=total_count,
            offset=offset,
        )

    def dashboard_metrics(self, today: date) -> DashboardMetrics:
        day_start = _to_iso(datetime.combine(today, time.min))
        day_end = _to_iso(datetime.combine(today + timedelta(days=1), time.min))
        seven_day_end = _to_iso(datetime.combine(today + timedelta(days=7), time.min))
        with self.database.connection() as connection:
            task_total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE is_paused = 0 AND is_archived = 0"
                ).fetchone()[0]
            )
            row = connection.execute(
                """
                SELECT
                    COUNT(s.id) AS schedule_total,
                    COALESCE(SUM(s.status = 'pending'), 0) AS pending_count,
                    COALESCE(SUM(s.status = 'pending' AND s.scheduled_at < ?), 0)
                        AS overdue_count,
                    COALESCE(SUM(
                        s.status = 'pending'
                        AND s.scheduled_at >= ?
                        AND s.scheduled_at < ?
                    ), 0) AS due_today_count,
                    COALESCE(SUM(
                        s.status = 'pending'
                        AND s.scheduled_at >= ?
                        AND s.scheduled_at < ?
                    ), 0) AS next_seven_days_count,
                    COALESCE(SUM(s.status = 'completed'), 0) AS completed_count,
                    COALESCE(SUM(s.status = 'skipped'), 0) AS skipped_count
                FROM review_schedules AS s
                JOIN tasks AS t ON t.id = s.task_id
                WHERE t.is_paused = 0 AND t.is_archived = 0
                """,
                (day_start, day_start, day_end, day_start, seven_day_end),
            ).fetchone()
        return DashboardMetrics(
            task_total=task_total,
            schedule_total=int(row["schedule_total"]),
            pending_count=int(row["pending_count"]),
            overdue_count=int(row["overdue_count"]),
            due_today_count=int(row["due_today_count"]),
            next_seven_days_count=int(row["next_seven_days_count"]),
            completed_count=int(row["completed_count"]),
            skipped_count=int(row["skipped_count"]),
        )

    def update(self, task: Task, *, now: datetime | None = None) -> Task:
        if task.id is None:
            raise RepositoryError("更新任務時必須提供 ID。")
        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET name = ?, description = ?, category_id = ?, start_at = ?,
                        schedule_mode = ?, review_count = ?, is_paused = ?, paused_on = ?,
                        is_archived = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        task.name.strip(),
                        task.description,
                        task.category_id,
                        _to_iso(task.start_at),
                        task.schedule_mode.value,
                        task.review_count,
                        int(task.is_paused),
                        task.paused_on.isoformat() if task.paused_on else None,
                        int(task.is_archived),
                        timestamp,
                        task.id,
                    ),
                )
                _require_changed(cursor, "任務", task.id)
                _refresh_task_metrics(connection, task.id, timestamp_value.date(), timestamp)
        except sqlite3.Error as exc:
            raise RepositoryError("更新任務失敗。") from exc
        return self.get(task.id)

    def update_with_schedules(
        self,
        task: Task,
        schedule_times: Iterable[datetime],
        *,
        tag_ids: Iterable[int] = (),
        today: date,
        now: datetime | None = None,
    ) -> Task:
        """Atomically update task fields, tags, and only the pending schedule rows."""

        if task.id is None:
            raise RepositoryError("更新任務時必須提供 ID。")
        name = task.name.strip()
        if not name:
            raise RepositoryError("任務名稱不可空白。")
        times = validate_manual_schedule(task.start_at, schedule_times)
        if task.schedule_mode is ScheduleMode.CURVE and len(times) != task.review_count:
            raise RepositoryError("遺忘曲線排程數量與複習次數不一致。")
        if task.schedule_mode is ScheduleMode.MANUAL and task.review_count is not None:
            raise RepositoryError("手動排程不可設定遺忘曲線複習次數。")

        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        unique_tag_ids = tuple(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        try:
            with self.database.transaction() as connection:
                _require_task(connection, task.id)
                existing = _fetch_schedules(connection, task.id)
                rebuilt = rebuild_pending_schedules(
                    existing,
                    times,
                    task_id=task.id,
                    start_at=task.start_at,
                )
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET name = ?, description = ?, category_id = ?, start_at = ?,
                        schedule_mode = ?, review_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        task.description,
                        task.category_id,
                        _to_iso(task.start_at),
                        task.schedule_mode.value,
                        task.review_count,
                        timestamp,
                        task.id,
                    ),
                )
                _require_changed(cursor, "任務", task.id)
                connection.execute("DELETE FROM task_tags WHERE task_id = ?", (task.id,))
                connection.executemany(
                    "INSERT INTO task_tags(task_id, tag_id) VALUES (?, ?)",
                    ((task.id, tag_id) for tag_id in unique_tag_ids),
                )
                connection.execute(
                    "DELETE FROM review_schedules WHERE task_id = ? AND status = 'pending'",
                    (task.id,),
                )
                connection.executemany(
                    """
                    INSERT INTO review_schedules(
                        task_id, sequence, scheduled_at, status, processed_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', NULL, ?, ?)
                    """,
                    (
                        (
                            task.id,
                            item.sequence,
                            _to_iso(item.scheduled_at),
                            timestamp,
                            timestamp,
                        )
                        for item in rebuilt
                        if item.status is ScheduleStatus.PENDING
                    ),
                )
                _refresh_task_metrics(connection, task.id, today, timestamp)
        except sqlite3.Error as exc:
            raise RepositoryError("更新任務、標籤與排程失敗，原資料未變更。") from exc
        return self.get(task.id)

    def replace_tags(self, task_id: int, tag_ids: Iterable[int]) -> None:
        unique_tag_ids = tuple(dict.fromkeys(int(tag_id) for tag_id in tag_ids))
        try:
            with self.database.transaction() as connection:
                _require_task(connection, task_id)
                connection.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
                connection.executemany(
                    "INSERT INTO task_tags(task_id, tag_id) VALUES (?, ?)",
                    ((task_id, tag_id) for tag_id in unique_tag_ids),
                )
        except sqlite3.Error as exc:
            raise RepositoryError("更新任務標籤失敗。") from exc

    def tag_ids_for_task(self, task_id: int) -> tuple[int, ...]:
        with self.database.connection() as connection:
            _require_task(connection, task_id)
            rows = connection.execute(
                "SELECT tag_id FROM task_tags WHERE task_id = ? ORDER BY tag_id", (task_id,)
            ).fetchall()
        return tuple(int(row["tag_id"]) for row in rows)

    def tag_ids_by_task(self, task_ids: Iterable[int]) -> dict[int, tuple[int, ...]]:
        ids = tuple(dict.fromkeys(int(task_id) for task_id in task_ids))
        result: dict[int, list[int]] = {task_id: [] for task_id in ids}
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT task_id, tag_id
                FROM task_tags
                WHERE task_id IN ({placeholders})
                ORDER BY task_id, tag_id
                """,  # noqa: S608
                ids,
            ).fetchall()
        for row in rows:
            result[int(row["task_id"])].append(int(row["tag_id"]))
        return {task_id: tuple(tag_ids) for task_id, tag_ids in result.items()}

    def set_paused(self, task_id: int, *, paused_on: date, now: datetime | None = None) -> Task:
        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET is_paused = 1, paused_on = ?, updated_at = ?
                WHERE id = ?
                """,
                (paused_on.isoformat(), timestamp, task_id),
            )
            _require_changed(cursor, "任務", task_id)
            _refresh_task_metrics(connection, task_id, paused_on, timestamp)
        return self.get(task_id)

    def resume(self, task_id: int, *, resumed_on: date, now: datetime | None = None) -> Task:
        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        with self.database.transaction() as connection:
            task_row = _require_task(connection, task_id)
            if not bool(task_row["is_paused"]) or task_row["paused_on"] is None:
                raise RepositoryError("任務目前不是暫停狀態。")
            schedules = _fetch_schedules(connection, task_id)
            shifted = resume_after_pause(
                schedules,
                paused_on=date.fromisoformat(task_row["paused_on"]),
                resumed_on=resumed_on,
            )
            pending_shifted = sorted(
                (item for item in shifted if item.status is ScheduleStatus.PENDING),
                key=lambda item: item.scheduled_at,
                reverse=True,
            )
            connection.executemany(
                "UPDATE review_schedules SET scheduled_at = ?, updated_at = ? WHERE id = ?",
                ((_to_iso(item.scheduled_at), timestamp, item.id) for item in pending_shifted),
            )
            connection.execute(
                """
                UPDATE tasks
                SET is_paused = 0, paused_on = NULL, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, task_id),
            )
            _refresh_task_metrics(connection, task_id, resumed_on, timestamp)
        return self.get(task_id)

    def set_archived(self, task_id: int, *, archived: bool, now: datetime | None = None) -> Task:
        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET is_archived = ?, updated_at = ? WHERE id = ?",
                (int(archived), timestamp, task_id),
            )
            _require_changed(cursor, "任務", task_id)
            _refresh_task_metrics(connection, task_id, timestamp_value.date(), timestamp)
        return self.get(task_id)

    def delete(self, task_id: int) -> None:
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            _require_changed(cursor, "任務", task_id)

    def refresh_metrics(self, task_id: int, *, today: date, now: datetime | None = None) -> Task:
        timestamp = _to_iso(now or datetime.now())
        with self.database.transaction() as connection:
            _refresh_task_metrics(connection, task_id, today, timestamp)
        return self.get(task_id)

    def refresh_all_metrics(self, *, today: date, now: datetime | None = None) -> None:
        timestamp = _to_iso(now or datetime.now())
        with self.database.transaction() as connection:
            day_start = _to_iso(datetime.combine(today, time.min))
            day_end = _to_iso(datetime.combine(today + timedelta(days=1), time.min))
            rows = connection.execute(
                """
                SELECT
                    t.id,
                    t.start_at,
                    t.status,
                    t.completion_rate,
                    t.is_archived,
                    t.is_paused,
                    COUNT(s.id) AS schedule_count,
                    COALESCE(SUM(s.status = 'pending'), 0) AS pending_count,
                    COALESCE(SUM(s.status = 'completed'), 0) AS completed_count,
                    COALESCE(SUM(s.status = 'skipped'), 0) AS skipped_count,
                    COALESCE(SUM(s.status = 'pending' AND s.scheduled_at < ?), 0)
                        AS overdue_count,
                    COALESCE(SUM(
                        s.status = 'pending'
                        AND s.scheduled_at >= ?
                        AND s.scheduled_at < ?
                    ), 0) AS due_today_count
                FROM tasks AS t
                LEFT JOIN review_schedules AS s ON s.task_id = t.id
                GROUP BY t.id
                """,
                (day_start, day_start, day_end),
            ).fetchall()
            updates: list[tuple[str, float, str, int]] = []
            for row in rows:
                schedule_count = int(row["schedule_count"])
                completed_count = int(row["completed_count"])
                skipped_count = int(row["skipped_count"])
                pending_count = int(row["pending_count"])
                if bool(row["is_archived"]):
                    status = TaskStatus.ARCHIVED
                elif bool(row["is_paused"]):
                    status = TaskStatus.PAUSED
                elif schedule_count and completed_count == schedule_count:
                    status = TaskStatus.COMPLETED
                elif not pending_count and skipped_count:
                    status = TaskStatus.INCOMPLETE
                elif int(row["overdue_count"]):
                    status = TaskStatus.OVERDUE
                elif int(row["due_today_count"]):
                    status = TaskStatus.DUE_TODAY
                elif datetime.fromisoformat(row["start_at"]).date() > today:
                    status = TaskStatus.NOT_STARTED
                else:
                    status = TaskStatus.IN_PROGRESS
                rate = completed_count / schedule_count * 100.0 if schedule_count else 0.0
                if (
                    status.value != row["status"]
                    or abs(rate - float(row["completion_rate"])) > 1e-9
                ):
                    updates.append((status.value, rate, timestamp, int(row["id"])))
            connection.executemany(
                "UPDATE tasks SET status = ?, completion_rate = ?, updated_at = ? WHERE id = ?",
                updates,
            )


class ReviewRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_for_task(self, task_id: int) -> tuple[ReviewSchedule, ...]:
        with self.database.connection() as connection:
            return _fetch_schedules(connection, task_id)

    def get(self, schedule_id: int) -> ReviewSchedule:
        with self.database.connection() as connection:
            return _require_schedule(connection, schedule_id)

    def list_for_tasks(self, task_ids: Iterable[int]) -> tuple[ReviewSchedule, ...]:
        ids = tuple(dict.fromkeys(int(task_id) for task_id in task_ids))
        if not ids:
            return ()
        placeholders = ", ".join("?" for _ in ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM review_schedules
                WHERE task_id IN ({placeholders})
                ORDER BY task_id, sequence, id
                """,  # noqa: S608
                ids,
            ).fetchall()
        return tuple(_schedule_from_row(row) for row in rows)

    def records_for_task(self, task_id: int) -> tuple[ReviewRecord, ...]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM review_records WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def complete(self, schedule_id: int, *, occurred_at: datetime, today: date) -> ReviewSchedule:
        return self._finish(schedule_id, occurred_at=occurred_at, today=today, skipped=False)

    def skip(self, schedule_id: int, *, occurred_at: datetime, today: date) -> ReviewSchedule:
        return self._finish(schedule_id, occurred_at=occurred_at, today=today, skipped=True)

    def _finish(
        self,
        schedule_id: int,
        *,
        occurred_at: datetime,
        today: date,
        skipped: bool,
    ) -> ReviewSchedule:
        timestamp = _to_iso(occurred_at)
        with self.database.transaction() as connection:
            schedule = _require_schedule(connection, schedule_id)
            transition = (
                skip_review(schedule, occurred_at)
                if skipped
                else complete_review(schedule, occurred_at)
            )
            cursor = connection.execute(
                """
                UPDATE review_schedules
                SET status = ?, processed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (transition.schedule.status.value, timestamp, timestamp, schedule_id),
            )
            if cursor.rowcount != 1:
                raise RepositoryError("複習項目已由其他操作處理。")
            _insert_record(connection, transition.record)
            _refresh_task_metrics(connection, schedule.task_id, today, timestamp)
        return transition.schedule

    def postpone(
        self, schedule_id: int, *, occurred_at: datetime, today: date
    ) -> tuple[ReviewSchedule, ...]:
        timestamp = _to_iso(occurred_at)
        with self.database.transaction() as connection:
            selected = _require_schedule(connection, schedule_id)
            schedules = _fetch_schedules(connection, selected.task_id)
            result = postpone_to_tomorrow(
                schedules,
                current_schedule_id=schedule_id,
                occurred_at=occurred_at,
            )
            if result.removed_schedule_ids:
                placeholders = ", ".join("?" for _ in result.removed_schedule_ids)
                connection.execute(
                    f"DELETE FROM review_schedules WHERE id IN ({placeholders})",  # noqa: S608
                    result.removed_schedule_ids,
                )
            pending_schedules = sorted(
                (
                    item
                    for item in result.schedules
                    if item.id is not None and item.status is ScheduleStatus.PENDING
                ),
                key=lambda item: item.scheduled_at,
                reverse=True,
            )
            connection.executemany(
                "UPDATE review_schedules SET scheduled_at = ?, updated_at = ? WHERE id = ?",
                ((_to_iso(item.scheduled_at), timestamp, item.id) for item in pending_schedules),
            )
            for record in result.records:
                _insert_record(connection, record)
            _refresh_task_metrics(connection, selected.task_id, today, timestamp)
        return result.schedules

    def reschedule(
        self,
        schedule_id: int,
        *,
        scheduled_at: datetime,
        today: date,
        now: datetime | None = None,
    ) -> ReviewSchedule:
        timestamp = _to_iso(now or datetime.now())
        try:
            with self.database.transaction() as connection:
                schedule = _require_schedule(connection, schedule_id)
                if schedule.status is not ScheduleStatus.PENDING:
                    raise RepositoryError("已完成或已略過的複習不可修改日期。")
                task = _task_from_row(_require_task(connection, schedule.task_id))
                normalized = validate_manual_schedule(task.start_at, (scheduled_at,))[0]
                connection.execute(
                    """
                    UPDATE review_schedules
                    SET scheduled_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_to_iso(normalized), timestamp, schedule_id),
                )
                _refresh_task_metrics(connection, schedule.task_id, today, timestamp)
        except sqlite3.IntegrityError as exc:
            raise RepositoryError("同一任務不可有重複的複習時間。") from exc
        return self.get(schedule_id)

    def rebuild_pending(
        self,
        task_id: int,
        proposed_times: Iterable[datetime],
        *,
        start_at: datetime,
        schedule_mode: ScheduleMode,
        review_count: int | None,
        today: date,
        now: datetime | None = None,
    ) -> tuple[ReviewSchedule, ...]:
        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        with self.database.transaction() as connection:
            _require_task(connection, task_id)
            existing = _fetch_schedules(connection, task_id)
            rebuilt = rebuild_pending_schedules(
                existing,
                proposed_times,
                task_id=task_id,
                start_at=start_at,
            )
            if schedule_mode is ScheduleMode.CURVE and review_count != len(rebuilt):
                raise RepositoryError("遺忘曲線排程數量與複習次數不一致。")
            if schedule_mode is ScheduleMode.MANUAL and review_count is not None:
                raise RepositoryError("手動排程不可設定遺忘曲線複習次數。")
            connection.execute(
                "DELETE FROM review_schedules WHERE task_id = ? AND status = 'pending'",
                (task_id,),
            )
            connection.executemany(
                """
                INSERT INTO review_schedules(
                    task_id, sequence, scheduled_at, status, processed_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (
                    (task_id, item.sequence, _to_iso(item.scheduled_at), timestamp, timestamp)
                    for item in rebuilt
                    if item.status is ScheduleStatus.PENDING
                ),
            )
            connection.execute(
                """
                UPDATE tasks
                SET start_at = ?, schedule_mode = ?, review_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (_to_iso(start_at), schedule_mode.value, review_count, timestamp, task_id),
            )
            _refresh_task_metrics(connection, task_id, today, timestamp)
            persisted = _fetch_schedules(connection, task_id)
        return persisted

    def list_due_today(
        self,
        today: date,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ReviewSchedule, ...]:
        start = _to_iso(datetime.combine(today, time.min))
        end = _to_iso(datetime.combine(today + timedelta(days=1), time.min))
        return self._list_active_pending(
            "s.scheduled_at >= ? AND s.scheduled_at < ?",
            (start, end),
            limit=limit,
            offset=offset,
        )

    def count_due_today(self, today: date) -> int:
        start = _to_iso(datetime.combine(today, time.min))
        end = _to_iso(datetime.combine(today + timedelta(days=1), time.min))
        return self._count_active_pending(
            "s.scheduled_at >= ? AND s.scheduled_at < ?", (start, end)
        )

    def list_overdue(
        self,
        today: date,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ReviewSchedule, ...]:
        start = _to_iso(datetime.combine(today, time.min))
        return self._list_active_pending("s.scheduled_at < ?", (start,), limit=limit, offset=offset)

    def count_overdue(self, today: date) -> int:
        start = _to_iso(datetime.combine(today, time.min))
        return self._count_active_pending("s.scheduled_at < ?", (start,))

    def list_pending_for_day(
        self,
        day: date,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ReviewSchedule, ...]:
        start = _to_iso(datetime.combine(day, time.min))
        end = _to_iso(datetime.combine(day + timedelta(days=1), time.min))
        return self._list_active_pending(
            "s.scheduled_at >= ? AND s.scheduled_at < ?",
            (start, end),
            limit=limit,
            offset=offset,
        )

    def count_pending_for_day(self, day: date) -> int:
        start = _to_iso(datetime.combine(day, time.min))
        end = _to_iso(datetime.combine(day + timedelta(days=1), time.min))
        return self._count_active_pending(
            "s.scheduled_at >= ? AND s.scheduled_at < ?", (start, end)
        )

    def calendar_metrics(
        self,
        start_day: date,
        end_day: date,
        *,
        today: date,
    ) -> tuple[CalendarMetrics, ...]:
        if end_day < start_day:
            raise RepositoryError("月曆結束日期不可早於開始日期。")
        start = _to_iso(datetime.combine(start_day, time.min))
        end = _to_iso(datetime.combine(end_day + timedelta(days=1), time.min))
        today_start = _to_iso(datetime.combine(today, time.min))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    substr(s.scheduled_at, 1, 10) AS schedule_day,
                    COUNT(*) AS schedule_count,
                    COALESCE(SUM(s.status = 'pending'), 0) AS pending_count,
                    COALESCE(SUM(s.status = 'completed'), 0) AS completed_count,
                    COALESCE(SUM(s.status = 'skipped'), 0) AS skipped_count,
                    COALESCE(SUM(
                        s.status = 'pending' AND s.scheduled_at < ?
                    ), 0) AS overdue_count
                FROM review_schedules AS s
                JOIN tasks AS t ON t.id = s.task_id
                WHERE t.is_paused = 0
                  AND t.is_archived = 0
                  AND s.scheduled_at >= ?
                  AND s.scheduled_at < ?
                GROUP BY substr(s.scheduled_at, 1, 10)
                ORDER BY schedule_day
                """,
                (today_start, start, end),
            ).fetchall()
        return tuple(
            CalendarMetrics(
                day=date.fromisoformat(row["schedule_day"]),
                schedule_count=int(row["schedule_count"]),
                pending_count=int(row["pending_count"]),
                completed_count=int(row["completed_count"]),
                skipped_count=int(row["skipped_count"]),
                overdue_count=int(row["overdue_count"]),
            )
            for row in rows
        )

    def list_active_between(
        self, start_day: date, end_day_exclusive: date
    ) -> tuple[ReviewSchedule, ...]:
        if end_day_exclusive < start_day:
            raise RepositoryError("查詢結束日期不可早於開始日期。")
        start = _to_iso(datetime.combine(start_day, time.min))
        end = _to_iso(datetime.combine(end_day_exclusive, time.min))
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT s.*
                FROM review_schedules AS s
                JOIN tasks AS t ON t.id = s.task_id
                WHERE t.is_paused = 0
                  AND t.is_archived = 0
                  AND s.scheduled_at >= ?
                  AND s.scheduled_at < ?
                ORDER BY s.scheduled_at, s.id
                """,
                (start, end),
            ).fetchall()
        return tuple(_schedule_from_row(row) for row in rows)

    def _list_active_pending(
        self,
        date_predicate: str,
        parameters: tuple[str, ...],
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ReviewSchedule, ...]:
        if limit is not None and (limit < 1 or limit > 500):
            raise RepositoryError("複習分頁數量不正確。")
        if offset < 0:
            raise RepositoryError("複習分頁位置不正確。")
        query = f"""
            SELECT s.*
            FROM review_schedules AS s
            JOIN tasks AS t ON t.id = s.task_id
            WHERE s.status = 'pending'
              AND t.is_paused = 0
              AND t.is_archived = 0
              AND {date_predicate}
            ORDER BY s.scheduled_at, s.id
        """
        query_parameters: tuple[object, ...] = parameters
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            query_parameters = (*parameters, limit, offset)
        with self.database.connection() as connection:
            rows = connection.execute(query, query_parameters).fetchall()
        return tuple(_schedule_from_row(row) for row in rows)

    def _count_active_pending(
        self,
        date_predicate: str,
        parameters: tuple[str, ...],
    ) -> int:
        query = f"""
            SELECT COUNT(*)
            FROM review_schedules AS s
            JOIN tasks AS t ON t.id = s.task_id
            WHERE s.status = 'pending'
              AND t.is_paused = 0
              AND t.is_archived = 0
              AND {date_predicate}
        """
        with self.database.connection() as connection:
            return int(connection.execute(query, parameters).fetchone()[0])


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def set(self, key: str, value: Any, *, now: datetime | None = None) -> None:
        timestamp = _to_iso(now or datetime.now())
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, payload, timestamp),
            )

    def get(self, key: str, default: Any = None) -> Any:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row[0])


class AssetRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, entry: AssetEntry, *, now: datetime | None = None) -> AssetEntry:
        timestamp_value = now or datetime.now()
        timestamp = _to_iso(timestamp_value)
        relative_path = _validate_relative_path(entry.relative_path)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO assets(kind, relative_path, enabled, scope_page, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.kind,
                    relative_path,
                    int(entry.enabled),
                    entry.scope_page,
                    timestamp,
                    timestamp,
                ),
            )
            asset_id = int(cursor.lastrowid)
        return AssetEntry(
            id=asset_id,
            kind=entry.kind,
            relative_path=relative_path,
            enabled=entry.enabled,
            scope_page=entry.scope_page,
            created_at=timestamp_value,
            updated_at=timestamp_value,
        )

    def list(self, kind: str | None = None) -> tuple[AssetEntry, ...]:
        if kind is not None and kind not in {"background", "sticker"}:
            raise RepositoryError("不支援的素材類型。")
        query = "SELECT * FROM assets"
        parameters: tuple[object, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            parameters = (kind,)
        query += " ORDER BY created_at, id"
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(_asset_from_row(row) for row in rows)

    def get(self, asset_id: int) -> AssetEntry:
        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError(f"找不到素材：{asset_id}")
        return _asset_from_row(row)

    def delete(self, asset_id: int) -> AssetEntry:
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                raise EntityNotFoundError(f"找不到素材：{asset_id}")
            connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return _asset_from_row(row)

    def delete_with_setting(
        self,
        asset_id: int,
        setting_key: str,
        setting_value: object,
        *,
        now: datetime | None = None,
    ) -> AssetEntry:
        timestamp = _to_iso(now or datetime.now())
        try:
            payload = json.dumps(setting_value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise RepositoryError("設定值無法轉換成 JSON。") from exc
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if row is None:
                raise EntityNotFoundError(f"找不到素材：{asset_id}")
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (setting_key, payload, timestamp),
            )
            connection.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return _asset_from_row(row)


class BackupLogRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, entry: BackupLogEntry) -> int:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO backup_logs(
                    operation, occurred_at, recipient, backup_filename,
                    backup_checksum, success, error_message, gmail_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.operation,
                    _to_iso(entry.occurred_at),
                    entry.recipient,
                    entry.backup_filename,
                    entry.backup_checksum,
                    int(entry.success),
                    entry.error_message,
                    entry.gmail_message_id,
                ),
            )
        return int(cursor.lastrowid)


def _refresh_task_metrics(
    connection: sqlite3.Connection, task_id: int, today: date, updated_at: str
) -> None:
    task_row = _require_task(connection, task_id)
    task = _task_from_row(task_row)
    schedules = _fetch_schedules(connection, task_id)
    status = calculate_task_status(
        start_at=task.start_at,
        schedules=schedules,
        today=today,
        is_archived=task.is_archived,
        is_paused=task.is_paused,
    )
    connection.execute(
        "UPDATE tasks SET status = ?, completion_rate = ?, updated_at = ? WHERE id = ?",
        (status.value, completion_rate(schedules), updated_at, task_id),
    )


def _fetch_schedules(connection: sqlite3.Connection, task_id: int) -> tuple[ReviewSchedule, ...]:
    rows = connection.execute(
        "SELECT * FROM review_schedules WHERE task_id = ? ORDER BY sequence, id", (task_id,)
    ).fetchall()
    return tuple(_schedule_from_row(row) for row in rows)


def _require_task(connection: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise EntityNotFoundError(f"找不到任務：{task_id}")
    return row


def _require_schedule(connection: sqlite3.Connection, schedule_id: int) -> ReviewSchedule:
    row = connection.execute(
        "SELECT * FROM review_schedules WHERE id = ?", (schedule_id,)
    ).fetchone()
    if row is None:
        raise EntityNotFoundError(f"找不到複習項目：{schedule_id}")
    return _schedule_from_row(row)


def _insert_record(connection: sqlite3.Connection, record: ReviewRecord) -> None:
    connection.execute(
        """
        INSERT INTO review_records(
            task_id, schedule_id, action, occurred_at, previous_scheduled_at,
            resulting_scheduled_at, merged_into_schedule_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.task_id,
            record.schedule_id,
            record.action.value,
            _to_iso(record.occurred_at),
            _to_iso(record.previous_scheduled_at) if record.previous_scheduled_at else None,
            _to_iso(record.resulting_scheduled_at) if record.resulting_scheduled_at else None,
            record.merged_into_schedule_id,
        ),
    )


def _task_from_row(row: sqlite3.Row) -> Task:
    return Task(
        id=int(row["id"]),
        name=str(row["name"]),
        description=str(row["description"]),
        category_id=int(row["category_id"]) if row["category_id"] is not None else None,
        start_at=datetime.fromisoformat(row["start_at"]),
        schedule_mode=ScheduleMode(row["schedule_mode"]),
        review_count=int(row["review_count"]) if row["review_count"] is not None else None,
        status=TaskStatus(row["status"]),
        completion_rate=float(row["completion_rate"]),
        is_paused=bool(row["is_paused"]),
        paused_on=date.fromisoformat(row["paused_on"]) if row["paused_on"] else None,
        is_archived=bool(row["is_archived"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _schedule_from_row(row: sqlite3.Row) -> ReviewSchedule:
    return ReviewSchedule(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        sequence=int(row["sequence"]),
        scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
        status=ScheduleStatus(row["status"]),
        processed_at=datetime.fromisoformat(row["processed_at"]) if row["processed_at"] else None,
    )


def _record_from_row(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        task_id=int(row["task_id"]),
        schedule_id=int(row["schedule_id"]) if row["schedule_id"] is not None else None,
        action=ReviewAction(row["action"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        previous_scheduled_at=(
            datetime.fromisoformat(row["previous_scheduled_at"])
            if row["previous_scheduled_at"]
            else None
        ),
        resulting_scheduled_at=(
            datetime.fromisoformat(row["resulting_scheduled_at"])
            if row["resulting_scheduled_at"]
            else None
        ),
        merged_into_schedule_id=(
            int(row["merged_into_schedule_id"])
            if row["merged_into_schedule_id"] is not None
            else None
        ),
    )


def _asset_from_row(row: sqlite3.Row) -> AssetEntry:
    return AssetEntry(
        id=int(row["id"]),
        kind=str(row["kind"]),
        relative_path=str(row["relative_path"]),
        enabled=bool(row["enabled"]),
        scope_page=str(row["scope_page"]) if row["scope_page"] is not None else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _require_changed(cursor: sqlite3.Cursor, entity_name: str, entity_id: int) -> None:
    if cursor.rowcount != 1:
        raise EntityNotFoundError(f"找不到{entity_name}：{entity_id}")


def _to_iso(value: datetime) -> str:
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise RepositoryError("資料庫只接受不含時區位移的本機時間。")
    return value.isoformat(timespec="microseconds")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _validate_relative_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RepositoryError("素材必須使用安全的相對路徑。")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise RepositoryError("素材相對路徑不可空白。")
    return normalized
