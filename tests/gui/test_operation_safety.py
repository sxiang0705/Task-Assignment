from datetime import datetime, timedelta
from threading import Event

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from task_assignment.app import create_application
from task_assignment.application import TaskDraft
from task_assignment.application.errors import ServiceError
from task_assignment.domain.enums import ScheduleMode


def test_queued_gmail_handoff_remains_busy_until_cleanup(qtbot, tmp_path, monkeypatch):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    page = window.page_widgets["backup"]
    scheduled = []
    monkeypatch.setattr(QTimer, "singleShot", lambda delay, callback: scheduled.append(callback))
    page._operation_active = True
    page._set_action_buttons(False)
    page._queued_operation = ("gmail_send", lambda: None)
    page._thread_finished()
    assert page._thread is None
    assert page.operation_in_progress
    assert len(scheduled) == 1
    assert not page.create_button.isEnabled()
    page._finish_draft_without_send()
    assert not page.operation_in_progress
    assert page.create_button.isEnabled()


def test_import_blocks_edits_close_and_competing_worker(qtbot, tmp_path, monkeypatch):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    window.show()
    window.show_page_by_id("backup")
    backup = window.page_widgets["backup"]
    settings = window.page_widgets["settings"]
    release = Event()
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)

    def operation():
        release.wait(10)
        raise ServiceError("測試匯入失敗")

    try:
        backup._start_operation("import", operation)
        assert not window.navigation_frame.isEnabled()
        assert not window.page_widgets["tasks"].isEnabled()
        window.show_page_by_id("tasks")
        assert window.page_stack.currentWidget() is backup
        assert not window.close()
        assert window.isVisible()
        settings._start_gmail_account_operation(lambda: None)
        assert settings._gmail_thread is None
    finally:
        release.set()
        qtbot.waitUntil(lambda: backup._thread is None, timeout=5000)
    assert window.navigation_frame.isEnabled()
    assert window.page_widgets["tasks"].isEnabled()
    assert window._operation_owner is None
    assert window.close()


def test_restart_lock_survives_worker_cleanup(qtbot, tmp_path, monkeypatch):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    backup = window.page_widgets["backup"]
    assert window.try_start_operation(backup, "import")
    window._lock_for_restart(None)
    monkeypatch.setattr(backup, "refresh", lambda: (_ for _ in ()).throw(AssertionError()))
    backup._thread_finished()
    assert not window.page_stack.isEnabled()
    assert not window.navigation_frame.isEnabled()
    assert window._operation_owner is None
    assert not window.try_start_operation(backup, "backup")
    assert window.close()


def test_midnight_refresh_is_deferred_until_worker_finishes(qtbot, tmp_path):
    now = datetime(2026, 9, 8, 23, 59)
    _, window = create_application([], data_dir=tmp_path, now_provider=lambda: now)
    qtbot.addWidget(window)
    window.services.tasks.create(TaskDraft(
        name="跨日複習", start_at=now, schedule_mode=ScheduleMode.MANUAL,
        manual_schedule_times=(now,),
    ))
    window.refresh_all_pages()
    owner = window.page_widgets["backup"]
    assert window.try_start_operation(owner, "backup")
    now += timedelta(minutes=2)
    window._check_date_change()
    assert window._last_local_date != now.date()
    window.finish_operation(owner)
    assert window._last_local_date == now.date()
    page = window.page_widgets["reviews"]
    assert page.hero.today == 0
    assert page.hero.overdue == 1
