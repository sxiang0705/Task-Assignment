from datetime import datetime, timedelta

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

from task_assignment.app import create_application
from task_assignment.application import TaskDraft
from task_assignment.application.errors import ServiceError
from task_assignment.domain.enums import ScheduleMode, ScheduleStatus
from task_assignment.ui.learning_visuals import CardDeparture, CompletionFeedback, ReviewPath


def test_complete_commits_before_feedback_and_can_repeat_after_animation(qtbot, tmp_path):
    now = datetime(2026, 9, 8, 12)
    app, window = create_application([], data_dir=tmp_path, now_provider=lambda: now)
    qtbot.addWidget(window)
    details = window.services.tasks.create(
        TaskDraft(
            name="複習回饋",
            start_at=now - timedelta(days=2),
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(now - timedelta(days=1), now),
        )
    )
    window.refresh_all_pages()
    window.show()
    page = window.page_widgets["reviews"]
    page.table.selectRow(0)
    page.complete_button.click()
    assert (
        window.services.tasks.details(details.task.id).schedules[0].status
        == ScheduleStatus.COMPLETED
    )
    assert page.table.rowCount() == 1
    assert page.table.viewport().findChildren(CardDeparture)
    assert page.table.viewport().findChildren(CompletionFeedback)
    qtbot.waitUntil(
        lambda: not page.table.viewport().findChildren(CompletionFeedback), timeout=2500
    )
    page.table.selectRow(0)
    page.complete_button.click()
    assert page.table.rowCount() == 0
    assert all(
        item.status == ScheduleStatus.COMPLETED
        for item in window.services.tasks.details(details.task.id).schedules
    )


def test_review_path_uses_actual_dates_and_off_mode(qtbot, tmp_path):
    now = datetime(2026, 9, 8, 12)
    _, window = create_application([], data_dir=tmp_path, now_provider=lambda: now)
    qtbot.addWidget(window)
    window.services.settings.set("interface_motion", "off")
    details = window.services.tasks.create(
        TaskDraft(
            name="自訂路徑",
            start_at=now,
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(now + timedelta(days=2), now + timedelta(days=17)),
        )
    )
    path = ReviewPath(details.schedules, today=now.date(), parent=window)
    qtbot.addWidget(path)
    window.show()
    path.show()
    assert [item.scheduled_at for item in path.schedules] == [
        now + timedelta(days=2),
        now + timedelta(days=17),
    ]
    assert path.progress == 1


def test_failed_completion_never_celebrates(qtbot, tmp_path, monkeypatch):
    now = datetime(2026, 9, 8, 12)
    _, window = create_application([], data_dir=tmp_path, now_provider=lambda: now)
    qtbot.addWidget(window)
    window.services.tasks.create(
        TaskDraft(
            name="失敗測試",
            start_at=now,
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(now,),
        )
    )
    page = window.page_widgets["reviews"]
    page.refresh()
    page.table.selectRow(0)
    errors = []

    def fail(_schedule_id):
        raise ServiceError("無法儲存")

    monkeypatch.setattr(window.services.reviews, "complete", fail)
    monkeypatch.setattr(page, "_show_error", lambda title, error: errors.append(str(error)))
    page._complete()
    assert errors == ["無法儲存"]
    assert page.table.rowCount() == 1
    assert not page.table.viewport().findChildren(CompletionFeedback)
    assert not page.table.viewport().findChildren(CardDeparture)


def test_departure_cancels_on_input_and_off_mode_has_no_snapshot(qtbot, tmp_path):
    now = datetime(2026, 9, 8, 12)
    _, window = create_application([], data_dir=tmp_path, now_provider=lambda: now)
    qtbot.addWidget(window)
    for index in range(3):
        window.services.tasks.create(TaskDraft(
            name=f"連續完成 {index}", start_at=now,
            schedule_mode=ScheduleMode.MANUAL, manual_schedule_times=(now,),
        ))
    window.refresh_all_pages()
    window.show()
    page = window.page_widgets["reviews"]
    page.table.selectRow(0)
    page._complete()
    overlay = page.table.viewport().findChildren(CardDeparture)[0]
    overlay.animation.pause()
    overlay._step(0.5)
    assert not overlay.grab().isNull()
    qtbot.mouseClick(page.table.viewport(), Qt.MouseButton.LeftButton)
    assert overlay.isHidden()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not page.table.viewport().findChildren(CardDeparture)
    page.table.selectRow(0)
    page._complete()
    assert page.table.rowCount() == 1
    window.services.settings.set("interface_motion", "off")
    window.apply_appearance()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not page.table.viewport().findChildren(CardDeparture)
    page.table.selectRow(0)
    page._complete()
    assert page.table.rowCount() == 0
    assert not page.table.viewport().findChildren(CardDeparture)
