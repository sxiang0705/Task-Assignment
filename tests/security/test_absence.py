from __future__ import annotations

import ast
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QAbstractButton, QWidget

from task_assignment.app import create_application
from task_assignment.application import TaskDraft
from task_assignment.domain.enums import ScheduleMode


def test_val_absence_001_002_004_005_008_source_has_no_removed_capabilities() -> None:
    """Removed v8/cloud/background capabilities are absent from production imports."""

    source_root = Path(__file__).resolve().parents[2] / "src" / "task_assignment"
    imported_roots: set[str] = set()
    identifiers: set[str] = set()
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                identifiers.add(node.id.casefold())
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.add(node.name.casefold())

    assert not imported_roots.intersection(
        {
            "apscheduler",
            "csv",
            "django",
            "fastapi",
            "flask",
            "plyer",
            "schedule",
            "win10toast",
        }
    )
    assert not {"difficulty", "difficulty_level", "review_rating"}.intersection(identifiers)


def test_val_absence_001_schema_has_no_difficulty_or_rating_fields(
    qtbot,
    tmp_path: Path,
) -> None:
    """VAL-ABSENCE-001: database schema contains no difficulty/rating inputs."""

    _, window = create_application([], data_dir=tmp_path / "absence-schema")
    qtbot.addWidget(window)
    with window.services.tasks.tasks.database.connection() as connection:
        columns = {
            str(row["name"]).casefold()
            for table in ("tasks", "review_schedules", "review_records")
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
    assert not {"difficulty", "difficulty_level", "rating"}.intersection(columns)


def test_val_absence_002_003_004_007_008_010_ui_has_no_forbidden_entry_points(
    qtbot,
    tmp_path: Path,
) -> None:
    """UI exposes replacement import but no CSV, merge, charts, login, or test controls."""

    application, window = create_application([], data_dir=tmp_path / "absence-ui")
    qtbot.addWidget(window)
    window.show()
    application.processEvents()
    buttons = window.findChildren(QAbstractButton)
    button_text = "\n".join(button.text() for button in buttons)
    object_names = "\n".join(button.objectName().casefold() for button in buttons)
    widget_classes = {widget.metaObject().className() for widget in window.findChildren(QWidget)}

    assert "匯入 ZIP（完整取代）" in button_text
    assert "CSV" not in button_text.upper()
    assert "合併匯入" not in button_text
    assert "桌面通知" not in button_text
    assert "帳號登入" not in button_text
    assert "多人協作" not in button_text
    assert "雲端同步" not in button_text
    assert "test" not in object_names
    assert "debug" not in object_names
    assert not any("chart" in class_name.casefold() for class_name in widget_classes)


def test_val_absence_005_close_leaves_no_running_qthread(qtbot, tmp_path: Path) -> None:
    """VAL-ABSENCE-005: an idle close leaves no reminder or Gmail worker running."""

    application, window = create_application([], data_dir=tmp_path / "absence-close")
    qtbot.addWidget(window)
    window.show()
    application.processEvents()
    window.close()
    application.processEvents()

    assert not any(thread.isRunning() for thread in window.findChildren(QThread))


def test_val_absence_006_completing_review_does_not_create_or_reschedule(
    qtbot,
    tmp_path: Path,
) -> None:
    """VAL-ABSENCE-006: completion records history without adaptive scheduling."""

    _, window = create_application([], data_dir=tmp_path / "absence-adaptive")
    qtbot.addWidget(window)
    details = window.services.tasks.create(
        TaskDraft(
            name="Fixed plan",
            start_at=datetime(2026, 9, 1, 9),
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(
                datetime(2026, 9, 2, 9),
                datetime(2026, 9, 3, 9),
            ),
        )
    )
    original_times = tuple(item.scheduled_at for item in details.schedules)

    window.services.reviews.complete(details.schedules[0].id)
    refreshed = window.services.tasks.details(details.task.id)

    assert len(refreshed.schedules) == 2
    assert tuple(item.scheduled_at for item in refreshed.schedules) == original_times
