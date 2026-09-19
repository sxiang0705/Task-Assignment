from __future__ import annotations

from datetime import datetime, timedelta
from time import perf_counter

import pytest
from PySide6.QtWidgets import QApplication

from task_assignment.application import TaskQuery
from task_assignment.application.dto import TaskSort
from task_assignment.application.errors import ServiceError
from task_assignment.application.services import create_services
from task_assignment.config import AppPaths
from task_assignment.domain.enums import TaskStatus
from task_assignment.infrastructure.database import Database
from task_assignment.infrastructure.gmail import MemoryCredentialStore
from task_assignment.ui.app_shell import AppShell
from task_assignment.ui.pages import CalendarPage, TaskManagementPage, TodayTasksPage

TASK_COUNT = 5_000
SCHEDULES_PER_TASK = 10
SCHEDULE_COUNT = TASK_COUNT * SCHEDULES_PER_TASK
PAGE_SIZE = 100
NOW = datetime(2026, 9, 6, 12)
CATEGORY_COUNT = 100
TAG_COUNT = 500
TASK_PROFILES = (
    {
        "status": "not_started",
        "completion_rate": 0,
        "start_offset": 1,
        "is_paused": 0,
        "is_archived": 0,
        "offsets": tuple(range(1, 11)),
        "schedule_statuses": ("pending",) * 10,
    },
    {
        "status": "in_progress",
        "completion_rate": 10,
        "start_offset": -30,
        "is_paused": 0,
        "is_archived": 0,
        "offsets": (-10, 1, 2, 3, 4, 5, 6, 7, 8, 9),
        "schedule_statuses": ("completed",) + ("pending",) * 9,
    },
    {
        "status": "due_today",
        "completion_rate": 10,
        "start_offset": -30,
        "is_paused": 0,
        "is_archived": 0,
        "offsets": (-10, -9, 0, 0, 1, 2, 3, 4, 5, 6),
        "schedule_statuses": ("completed", "skipped") + ("pending",) * 8,
    },
    {
        "status": "overdue",
        "completion_rate": 10,
        "start_offset": -30,
        "is_paused": 0,
        "is_archived": 0,
        "offsets": (-10, -9, -2, 0, 0, 1, 2, 3, 4, 5),
        "schedule_statuses": ("completed", "skipped") + ("pending",) * 8,
    },
    {
        "status": "completed",
        "completion_rate": 100,
        "start_offset": -30,
        "is_paused": 0,
        "is_archived": 0,
        "offsets": tuple(range(-10, 0)),
        "schedule_statuses": ("completed",) * 10,
    },
    {
        "status": "incomplete",
        "completion_rate": 90,
        "start_offset": -30,
        "is_paused": 0,
        "is_archived": 0,
        "offsets": tuple(range(-10, 0)),
        "schedule_statuses": ("completed",) * 9 + ("skipped",),
    },
    {
        "status": "paused",
        "completion_rate": 10,
        "start_offset": -30,
        "is_paused": 1,
        "is_archived": 0,
        "offsets": (-10, -9, -2, 0, 0, 1, 2, 3, 4, 5),
        "schedule_statuses": ("completed", "skipped") + ("pending",) * 8,
    },
    {
        "status": "archived",
        "completion_rate": 10,
        "start_offset": -30,
        "is_paused": 0,
        "is_archived": 1,
        "offsets": (-10, -9, 0, 0, 1, 2, 3, 4, 5, 6),
        "schedule_statuses": ("completed", "skipped") + ("pending",) * 8,
    },
)


@pytest.fixture(scope="module")
def scale_context(tmp_path_factory):
    paths = AppPaths.from_base_dir(tmp_path_factory.mktemp("scale-data"))
    paths.ensure_directories()
    database = Database(paths.database)
    database.initialize()
    _seed_scale_data(database)
    services = create_services(
        database,
        paths=paths,
        now_provider=lambda: NOW,
        credential_store=MemoryCredentialStore(),
    )
    return paths, database, services


@pytest.mark.parametrize("sort_by", list(TaskSort))
@pytest.mark.parametrize("descending", [False, True])
def test_focus_page_matches_sorted_results_without_loading_all(
    scale_context, monkeypatch, sort_by, descending,
):
    _, _, services = scale_context
    query = TaskQuery(sort_by=sort_by, descending=descending)
    expected = services.tasks.query_page(query, limit=100, offset=4000)
    target = expected.items[37].id

    def forbidden(*args, **kwargs):
        raise AssertionError("Full task loading is forbidden during navigation")

    monkeypatch.setattr(services.tasks, "query", forbidden)
    monkeypatch.setattr(services.tasks.tasks, "list_all", forbidden)
    started = perf_counter()
    actual = services.tasks.query_page(query, focus_task_id=target)
    elapsed = perf_counter() - started
    assert actual == expected
    assert elapsed < 1.0


def test_focus_missing_or_filtered_task_reports_error(scale_context):
    _, _, services = scale_context
    with pytest.raises(ServiceError):
        services.tasks.query_page(focus_task_id=999999)
    with pytest.raises(ServiceError):
        services.tasks.query_page(TaskQuery(text="no matching tasks"), focus_task_id=1)


def test_management_focus_loads_only_one_page(scale_context, qtbot, monkeypatch):
    _, _, services = scale_context
    page = TaskManagementPage(services)
    qtbot.addWidget(page)
    page.search_edit.setText("needle")
    original = services.tasks.query_page
    calls = []

    def tracked(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unpaged query called")

    monkeypatch.setattr(services.tasks, "query_page", tracked)
    monkeypatch.setattr(services.tasks, "query", forbidden)
    page.select_task(4995)
    assert len(calls) == 1
    assert page._selected().id == 4995
    assert page.table.rowCount() <= 100
    assert page._page_index > 0
    assert not page.from_date.isEnabled()
    errors = []
    monkeypatch.setattr(page, "_show_error", lambda title, error: errors.append(str(error)))
    page.select_task(999999)
    assert errors
    assert page._selected() is None


def test_val_perf_002_dashboard_handles_5000_tasks_and_50000_schedules(
    scale_context,
) -> None:
    """VAL-PERF-002: aggregate dashboard load stays below two seconds."""

    _, _, services = scale_context
    started = perf_counter()
    summary = services.tasks.dashboard_summary()
    elapsed = perf_counter() - started

    assert summary.task_total == 3_750
    assert summary.pending_review_count == 21_875
    assert summary.overdue_count == 625
    assert summary.due_today_count == 2_500
    assert summary.next_seven_days_count == 16_875
    assert summary.completed_review_count == 13_750
    assert summary.incomplete_review_count == 23_750
    assert summary.completion_rate == pytest.approx(36.6666667)
    assert elapsed < 2.0, f"dashboard took {elapsed:.3f}s"


def test_val_perf_003_paged_search_and_filter_update_below_one_second(scale_context) -> None:
    """VAL-PERF-003: user-approved acceptance allows one second for queries."""

    _, _, services = scale_context
    search = TaskQuery(text="needle")
    category = TaskQuery(category_id=3)
    services.tasks.query_page(search, limit=PAGE_SIZE)
    services.tasks.query_page(category, limit=PAGE_SIZE)

    search_started = perf_counter()
    search_page = services.tasks.query_page(search, limit=PAGE_SIZE)
    search_elapsed = perf_counter() - search_started
    filter_started = perf_counter()
    filter_page = services.tasks.query_page(category, limit=PAGE_SIZE)
    filter_elapsed = perf_counter() - filter_started

    assert search_page.total_count == 50
    assert len(search_page.items) == 50
    assert filter_page.total_count == 50
    assert len(filter_page.items) == 50
    assert search_elapsed < 1.0, f"search took {search_elapsed:.3f}s"
    assert filter_elapsed < 1.0, f"filter took {filter_elapsed:.3f}s"


def test_val_perf_001_004_startup_and_large_lists_are_paginated(
    qtbot,
    scale_context,
) -> None:
    """VAL-PERF-001/004: local startup stays responsive and tables cap their rows."""

    paths, _, services = scale_context
    started = perf_counter()
    services.tasks.refresh_all_statuses()
    window = AppShell(paths=paths, services=services)
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    elapsed = perf_counter() - started

    today_page = window.page_widgets["reviews"]
    task_page = window.page_widgets["tasks"]
    calendar_page = window.page_widgets["calendar"]
    assert isinstance(today_page, TodayTasksPage)
    assert isinstance(task_page, TaskManagementPage)
    assert isinstance(calendar_page, CalendarPage)
    assert today_page.table.rowCount() == PAGE_SIZE
    assert task_page.table.rowCount() == PAGE_SIZE
    assert calendar_page.table.rowCount() == PAGE_SIZE
    assert today_page.next_page_button.isEnabled()
    assert task_page.next_page_button.isEnabled()
    assert calendar_page.next_page_button.isEnabled()
    assert elapsed < 10.0, f"local startup components took {elapsed:.3f}s"

    first_today_schedule = today_page.items[0].schedule_id
    today_page.next_page_button.click()
    assert today_page.items[0].schedule_id != first_today_schedule
    first_task = task_page.items[0].id
    task_page.next_page_button.click()
    assert task_page.items[0].id != first_task
    first_day_schedule = calendar_page.items[0].schedule_id
    calendar_page.next_page_button.click()
    assert calendar_page.items[0].schedule_id != first_day_schedule

    task_page.previous_page_button.click()
    search_started = perf_counter()
    task_page.search_edit.setText("needle")
    QApplication.processEvents()
    search_elapsed = perf_counter() - search_started
    assert task_page.table.rowCount() == 50
    assert search_elapsed < 1.0, f"UI search update took {search_elapsed:.3f}s"

    overdue_page = services.tasks.query_page(
        TaskQuery(statuses=frozenset((TaskStatus.OVERDUE,))),
        limit=PAGE_SIZE,
    )
    assert overdue_page.total_count == 625
    assert all(item.completion_rate == pytest.approx(10.0) for item in overdue_page.items)


def _seed_scale_data(database: Database) -> None:
    timestamp = NOW.isoformat(timespec="seconds")
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO categories(name, created_at) VALUES (?, ?)",
            (
                (
                    f"類別 {index}" if index % 2 == 0 else f"Category {index}",
                    timestamp,
                )
                for index in range(1, CATEGORY_COUNT + 1)
            ),
        )
        connection.executemany(
            "INSERT INTO tags(name, created_at) VALUES (?, ?)",
            (
                (f"標籤 {index}" if index % 2 == 0 else f"Tag {index}", timestamp)
                for index in range(1, TAG_COUNT + 1)
            ),
        )
        connection.executemany(
            """
            INSERT INTO tasks(
                name, description, category_id, start_at, schedule_mode,
                review_count, status, completion_rate, is_paused, paused_on,
                is_archived, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'manual', NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    f"任務 {index:05d}" if index % 2 == 0 else f"Task {index:05d}",
                    "needle 效能資料" if index % 100 == 0 else "一般測試資料",
                    (index % CATEGORY_COUNT) + 1,
                    (NOW + timedelta(days=TASK_PROFILES[index % 8]["start_offset"])).isoformat(
                        timespec="seconds"
                    ),
                    TASK_PROFILES[index % 8]["status"],
                    TASK_PROFILES[index % 8]["completion_rate"],
                    TASK_PROFILES[index % 8]["is_paused"],
                    NOW.date().isoformat() if TASK_PROFILES[index % 8]["is_paused"] else None,
                    TASK_PROFILES[index % 8]["is_archived"],
                    timestamp,
                    timestamp,
                )
                for index in range(TASK_COUNT)
            ),
        )
        connection.executemany(
            "INSERT INTO task_tags(task_id, tag_id) VALUES (?, ?)",
            ((task_id, ((task_id - 1) % TAG_COUNT) + 1) for task_id in range(1, TASK_COUNT + 1)),
        )
        connection.executemany(
            """
            INSERT INTO review_schedules(
                task_id, sequence, scheduled_at, status, processed_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    task_id,
                    sequence,
                    (NOW.replace(hour=9) + timedelta(days=offset, hours=sequence)).isoformat(
                        timespec="seconds"
                    ),
                    schedule_status,
                    timestamp if schedule_status != "pending" else None,
                    timestamp,
                    timestamp,
                )
                for task_id in range(1, TASK_COUNT + 1)
                for sequence, (offset, schedule_status) in enumerate(
                    zip(
                        TASK_PROFILES[(task_id - 1) % 8]["offsets"],
                        TASK_PROFILES[(task_id - 1) % 8]["schedule_statuses"],
                        strict=True,
                    ),
                    start=1,
                )
            ),
        )
