from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt

import task_assignment.app as app_module
from task_assignment.app import create_application
from task_assignment.domain.enums import ScheduleMode, TaskStatus
from task_assignment.domain.models import Task
from task_assignment.infrastructure.database.connection import Database, initialize_database
from task_assignment.infrastructure.database.repositories import TaskRepository
from task_assignment.ui.app_shell import DEFAULT_PAGE_ID, NAVIGATION_ITEMS


def test_val_app_003_first_start_creates_required_storage(qtbot, tmp_path: Path) -> None:
    """VAL-APP-003: first startup creates its database and storage folders."""

    application, window = create_application([], data_dir=tmp_path / "app-data")
    qtbot.addWidget(window)

    assert application.applicationName() == "Task Assignment"
    assert not application.windowIcon().isNull()
    assert window.paths.database.is_file()
    assert window.paths.assets.is_dir()
    assert window.paths.backups.is_dir()
    assert window.paths.logs.is_dir()


def test_val_gui_001_navigation_is_complete_and_selection_is_visible(qtbot, tmp_path: Path) -> None:
    """VAL-GUI-001: the shell exposes every required page and current state."""

    _, window = create_application([], data_dir=tmp_path / "app-data")
    qtbot.addWidget(window)
    window.show()

    assert len(window.navigation_buttons) == len(NAVIGATION_ITEMS)
    assert [button.text() for button in window.navigation_buttons.values()] == [
        "今日任務",
        "任務管理",
        "月曆",
        "儀表板",
        "資料與備份",
        "個人化設定",
    ]
    assert window.navigation_buttons[DEFAULT_PAGE_ID].isChecked()
    assert window.page_stack.currentIndex() == 0
    assert all(button.minimumHeight() >= 44 for button in window.navigation_buttons.values())

    qtbot.mouseClick(
        window.navigation_buttons["calendar"],
        Qt.MouseButton.LeftButton,
    )

    assert window.navigation_buttons["calendar"].isChecked()
    assert window.page_stack.currentIndex() == 2


def test_branding_is_task_assignment_and_brand_has_explicit_contrast(qtbot, tmp_path: Path) -> None:
    _, window = create_application([], data_dir=tmp_path / "app-data")
    qtbot.addWidget(window)
    window.show()

    assert window.windowTitle() == "Task Assignment"
    assert window.brand_label.text() == "Task Assignment"
    assert "QLabel#brandLabel" in window.styleSheet()
    assert "background-color: #202840" in window.styleSheet()
    assert "color: #f5f1ff" in window.styleSheet()


def test_window_can_open_at_minimum_supported_viewport(qtbot, tmp_path: Path) -> None:
    _, window = create_application([], data_dir=tmp_path / "app-data")
    qtbot.addWidget(window)
    window.resize(800, 560)
    window.show()

    assert window.isVisible()
    assert window.centralWidget() is not None


def test_val_app_004_invalid_database_is_preserved_and_reported(
    monkeypatch, tmp_path: Path
) -> None:
    """VAL-APP-004: an invalid existing DB is reported and never overwritten."""

    data_dir = tmp_path / "app-data"
    data_dir.mkdir()
    database = data_dir / "task_assignment.db"
    original = b"this is not sqlite"
    database.write_bytes(original)
    monkeypatch.setenv("TASK_ASSIGNMENT_DATA_DIR", str(data_dir))
    reported_messages: list[str] = []
    monkeypatch.setattr(
        app_module,
        "_show_startup_error",
        lambda message, argv: reported_messages.append(message),
    )

    assert app_module.main([]) == 1
    assert database.read_bytes() == original
    assert reported_messages and "無法開啟資料庫" in reported_messages[0]


def test_val_app_006_startup_does_not_leave_database_connection_open(qtbot, tmp_path: Path) -> None:
    """VAL-APP-006: the M0 bootstrap keeps no long-lived DB/worker resource."""

    _, window = create_application([], data_dir=tmp_path / "app-data")
    qtbot.addWidget(window)
    window.close()
    moved_database = window.paths.database.with_suffix(".moved")

    window.paths.database.rename(moved_database)

    assert moved_database.is_file()


def test_database_bootstrap_creates_migration_ledger(tmp_path: Path) -> None:
    database = tmp_path / "task_assignment.db"
    initialize_database(database)

    with sqlite3.connect(database) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()

    assert table == ("schema_migrations",)


def test_val_app_005_task_status_is_refreshed_during_startup(qtbot, tmp_path: Path) -> None:
    """VAL-APP-005/VAL-TASK-007: startup refreshes status before the first page loads."""

    data_dir = tmp_path / "app-data"
    database = Database(data_dir / "task_assignment.db")
    database.initialize()
    created = TaskRepository(database).create(
        Task(
            name="啟動刷新",
            start_at=datetime(2026, 1, 1, 9, 30),
            schedule_mode=ScheduleMode.MANUAL,
            review_count=None,
        ),
        (datetime(2026, 1, 10, 9, 30),),
        now=datetime(2026, 1, 1, 10),
    )
    assert created.status is TaskStatus.IN_PROGRESS

    _, window = create_application(
        [],
        data_dir=data_dir,
        now_provider=lambda: datetime(2026, 1, 10, 12),
    )
    qtbot.addWidget(window)

    assert window.services.tasks.details(created.id).task.status is TaskStatus.DUE_TODAY
    assert window.navigation_buttons[DEFAULT_PAGE_ID].isChecked()
