from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDate, QDateTime, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QMovie, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QPushButton,
)

from task_assignment.app import create_application
from task_assignment.application import TaskDraft, create_services
from task_assignment.config import AppPaths
from task_assignment.domain.enums import ScheduleMode
from task_assignment.infrastructure.database import Database
from task_assignment.infrastructure.gmail import (
    CredentialRecord,
    GmailNetworkError,
    MemoryCredentialStore,
    OAuthTokens,
)
from task_assignment.ui.app_shell import AppShell
from task_assignment.ui.pages import (
    BackupPage,
    CalendarPage,
    DashboardPage,
    GmailDraftDialog,
    PersonalizationPage,
    TaskManagementPage,
    TodayTasksPage,
)
from task_assignment.ui.task_dialog import TaskDialog

NOW = datetime(2026, 1, 10, 12)


class FakeGuiOAuth:
    def __init__(self) -> None:
        self.authorize_calls = 0
        self.revoke_calls = 0

    def authorize(self) -> OAuthTokens:
        self.authorize_calls += 1
        return OAuthTokens(
            access_token="access",
            refresh_token="refresh",
            account_email="owner@example.com",
        )

    def refresh(self, _refresh_token: str) -> OAuthTokens:
        return OAuthTokens(access_token="access")

    def revoke(self, _refresh_token: str) -> None:
        self.revoke_calls += 1


class FakeGuiGmail:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.error: Exception | None = None

    def send(self, **values: object) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append(values)
        return "gui-message-id"


def create_test_window(qtbot, tmp_path: Path):
    _, window = create_application([], data_dir=tmp_path / "app-data", now_provider=lambda: NOW)
    qtbot.addWidget(window)
    window.show()
    return window


def create_gmail_window(qtbot, tmp_path: Path, *, linked: bool = False):
    paths = AppPaths.from_base_dir(tmp_path / "gmail-app-data")
    paths.ensure_directories()
    database = Database(paths.database)
    database.initialize()
    credentials = MemoryCredentialStore(
        CredentialRecord("owner@example.com", "refresh") if linked else None
    )
    oauth = FakeGuiOAuth()
    gmail = FakeGuiGmail()
    services = create_services(
        database,
        paths=paths,
        now_provider=lambda: NOW,
        credential_store=credentials,
        oauth_gateway=oauth,
        gmail_gateway=gmail,
    )
    window = AppShell(paths=paths, services=services)
    qtbot.addWidget(window)
    window.show()
    return window, credentials, oauth, gmail


def active_gmail_draft_dialog() -> GmailDraftDialog | None:
    return next(
        (
            widget
            for widget in QApplication.topLevelWidgets()
            if isinstance(widget, GmailDraftDialog) and widget.isVisible()
        ),
        None,
    )


def manual_draft(name: str, *times: datetime) -> TaskDraft:
    return TaskDraft(
        name=name,
        start_at=datetime(2026, 1, 8, 9),
        schedule_mode=ScheduleMode.MANUAL,
        manual_schedule_times=times,
    )


def write_gui_image(path: Path, image_format: str, color: int) -> None:
    image = QImage(80, 60, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path), image_format)


def write_gui_gif(path: Path) -> None:
    """Write a tiny valid GIF so the packaged sticker playback path is exercised."""

    path.write_bytes(
        bytes.fromhex(
            "47494638396101000100800000000000ffffff"
            "2c00000000010001000002014c003b"
        )
    )


def test_m4_shell_uses_real_primary_pages(qtbot, tmp_path: Path) -> None:
    window = create_test_window(qtbot, tmp_path)

    assert isinstance(window.page_widgets["reviews"], TodayTasksPage)
    assert isinstance(window.page_widgets["tasks"], TaskManagementPage)
    assert isinstance(window.page_widgets["calendar"], CalendarPage)
    assert isinstance(window.page_widgets["dashboard"], DashboardPage)
    assert isinstance(window.page_widgets["backup"], BackupPage)


def test_val_gui_011_012_all_pages_have_help_and_one_add_task_entry(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """VAL-GUI-011/012: every page has help and task creation has one entry."""

    window = create_test_window(qtbot, tmp_path)
    shown_titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, title, _text: shown_titles.append(title),
    )

    for page in window.page_widgets.values():
        assert page.help_button.text() == "?"
        assert page.help_button.accessibleName().startswith("開啟")
        page.help_button.click()

    add_buttons = [
        button for button in window.findChildren(QPushButton) if button.text() == "＋ 新增任務"
    ]
    assert len(shown_titles) == 6
    assert len(add_buttons) == 1
    assert add_buttons[0] is window.page_widgets["tasks"].new_button


def test_val_backup_page_warns_and_creates_zip_without_blocking_ui(qtbot, tmp_path: Path) -> None:
    window = create_test_window(qtbot, tmp_path)
    page = window.page_widgets["backup"]
    assert isinstance(page, BackupPage)
    window.show_page_by_id("backup")

    visible_text = " ".join(label.text() for label in page.findChildren(QLabel))
    assert "ZIP 未加密" in visible_text
    assert "task-assignment-backup-YYYYMMDD-HHMMSS.zip" in visible_text

    page.create_button.click()
    assert page.operation_in_progress
    assert not page.create_button.isEnabled()
    qtbot.waitUntil(lambda: not page.operation_in_progress, timeout=5000)

    backups = list(window.paths.backups.glob("task-assignment-backup-*.zip"))
    assert len(backups) == 1
    assert "備份建立成功" in page.status_label.text()
    assert page.create_button.isEnabled()


def test_val_import_success_requires_restart_and_locks_data_ui(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window = create_test_window(qtbot, tmp_path)
    backup = window.services.backups.create_backup()
    page = window.page_widgets["backup"]
    assert isinstance(page, BackupPage)
    window.show_page_by_id("backup")
    notices: list[str] = []
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(backup.path), "Task Assignment 備份 (*.zip)"),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda _parent, _title, text: notices.append(text),
    )

    page.import_button.click()
    qtbot.waitUntil(lambda: not window.page_stack.isEnabled(), timeout=5000)
    qtbot.waitUntil(lambda: not page.operation_in_progress, timeout=5000)

    assert "請重新啟動" in window.windowTitle()
    assert not window.navigation_frame.isEnabled()
    assert not page.create_button.isEnabled()
    assert any("重新開啟" in notice for notice in notices)


def test_gmail_controls_explain_publisher_configuration_when_unavailable(
    qtbot, tmp_path: Path
) -> None:
    window = create_test_window(qtbot, tmp_path)
    backup_page = window.page_widgets["backup"]
    settings_page = window.page_widgets["settings"]
    assert isinstance(backup_page, BackupPage)
    assert isinstance(settings_page, PersonalizationPage)

    assert not backup_page.gmail_button.isEnabled()
    assert "此版本尚未開放 Gmail" in backup_page.gmail_status_label.text()
    assert not settings_page.gmail_connect_button.isEnabled()
    assert "不需要準備設定檔" in settings_page.gmail_account_status.text()
    for label in (backup_page.gmail_status_label, settings_page.gmail_account_status):
        assert "OAuth" not in label.text()
        assert "client" not in label.text()
    assert "連結不會寄出郵件" in settings_page.gmail_help_label.text()
    assert "不讀取收件匣" in settings_page.gmail_help_label.text()


def test_manual_gmail_flow_confirms_draft_sends_and_keeps_zip(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window, credentials, oauth, gmail = create_gmail_window(qtbot, tmp_path)
    page = window.page_widgets["backup"]
    assert isinstance(page, BackupPage)
    window.show_page_by_id("backup")
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)

    page.gmail_button.click()
    qtbot.waitUntil(lambda: active_gmail_draft_dialog() is not None, timeout=5000)
    dialog = active_gmail_draft_dialog()
    assert dialog is not None
    dialog.recipient_edit.setText("recipient@example.net")
    dialog.save_default_check.setChecked(True)
    dialog.accept()
    qtbot.waitUntil(lambda: "已寄送" in page.status_label.text(), timeout=5000)
    qtbot.waitUntil(lambda: not page.operation_in_progress, timeout=5000)

    assert oauth.authorize_calls == 1
    assert credentials.record == CredentialRecord("owner@example.com", "refresh")
    assert len(gmail.sent) == 1
    assert gmail.sent[0]["recipient"] == "recipient@example.net"
    assert gmail.sent[0]["attachment"].is_file()
    assert window.services.gmail.default_recipient() == "recipient@example.net"
    assert list(window.paths.backups.glob("task-assignment-backup-*.zip"))


def test_cancel_gmail_draft_keeps_created_zip_and_does_not_authorize(qtbot, tmp_path: Path) -> None:
    window, _, oauth, gmail = create_gmail_window(qtbot, tmp_path)
    page = window.page_widgets["backup"]
    assert isinstance(page, BackupPage)
    window.show_page_by_id("backup")

    page.gmail_button.click()
    qtbot.waitUntil(lambda: active_gmail_draft_dialog() is not None, timeout=5000)
    dialog = active_gmail_draft_dialog()
    assert dialog is not None
    dialog.reject()
    qtbot.waitUntil(lambda: "取消 Gmail 寄送" in page.status_label.text(), timeout=5000)
    qtbot.waitUntil(lambda: not page.operation_in_progress, timeout=5000)

    assert oauth.authorize_calls == 0
    assert not gmail.sent
    assert list(window.paths.backups.glob("task-assignment-backup-*.zip"))


def test_gmail_send_failure_returns_ui_to_idle_and_keeps_zip(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window, _, _, gmail = create_gmail_window(qtbot, tmp_path, linked=True)
    gmail.error = GmailNetworkError("Gmail 暫時無法使用。")
    page = window.page_widgets["backup"]
    assert isinstance(page, BackupPage)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    page.gmail_button.click()
    qtbot.waitUntil(lambda: active_gmail_draft_dialog() is not None, timeout=5000)
    dialog = active_gmail_draft_dialog()
    assert dialog is not None
    dialog.recipient_edit.setText("recipient@example.net")
    dialog.accept()
    qtbot.waitUntil(lambda: not page.operation_in_progress, timeout=5000)
    qtbot.waitUntil(lambda: bool(warnings), timeout=5000)

    assert "ZIP 仍保留" in page.detail_label.text()
    assert page.create_button.isEnabled()
    assert list(window.paths.backups.glob("task-assignment-backup-*.zip"))


def test_settings_page_links_and_disconnects_gmail_in_background(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window, credentials, oauth, _ = create_gmail_window(qtbot, tmp_path)
    page = window.page_widgets["settings"]
    assert isinstance(page, PersonalizationPage)
    window.show_page_by_id("settings")
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    assert page.gmail_connect_button.isEnabled()
    page.gmail_connect_button.click()
    qtbot.waitUntil(lambda: credentials.record is not None, timeout=5000)
    qtbot.waitUntil(lambda: page.gmail_disconnect_button.isEnabled(), timeout=5000)
    assert "owner@example.com" in page.gmail_account_status.text()

    page.gmail_disconnect_button.click()
    qtbot.waitUntil(lambda: credentials.record is None, timeout=5000)
    qtbot.waitUntil(lambda: page.gmail_connect_button.isEnabled(), timeout=5000)
    assert oauth.revoke_calls == 1
    assert "尚未連結" in page.gmail_account_status.text()


def test_val_schedule_004_005_007_task_dialog_live_preview(qtbot, tmp_path: Path) -> None:
    """VAL-SCHEDULE-004/005/007: form preview gates invalid schedules."""

    window = create_test_window(qtbot, tmp_path)
    dialog = TaskDialog(window.services.tasks, parent=window)
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("表單預覽")
    dialog.start_edit.setDateTime(QDateTime(datetime(2026, 1, 10, 9, 30)))
    dialog.review_count_spin.setValue(3)

    assert dialog.preview_list.count() == 3
    assert dialog.preview_list.item(0).text().endswith("2026-01-11 09:30")
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Save).isEnabled()

    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(ScheduleMode.MANUAL.value))
    for day in (13, 11):
        dialog.manual_picker.setDateTime(QDateTime(datetime(2026, 1, day, 9, 30)))
        dialog.manual_add_button.click()
    assert dialog.preview_list.item(0).text().endswith("2026-01-11 09:30")
    assert not dialog.manual_add_button.isEnabled()
    dialog.manual_add_button.click()
    assert dialog.preview_list.count() == 2
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Save).isEnabled()


def test_val_search_004_task_page_updates_result_count_while_typing(qtbot, tmp_path: Path) -> None:
    """VAL-SEARCH-004: task results and visible count update with the query."""

    window = create_test_window(qtbot, tmp_path)
    window.services.tasks.create(manual_draft("Alpha", datetime(2026, 1, 10, 9, 30)))
    window.services.tasks.create(manual_draft("Beta", datetime(2026, 1, 11, 9, 30)))
    page = window.page_widgets["tasks"]
    assert isinstance(page, TaskManagementPage)
    page.refresh()
    assert page.table.rowCount() == 2

    page.search_edit.setText("Alpha")

    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "Alpha"
    assert page.result_label.text() == "1 個任務"
    assert page.date_filter_check.text() == "只顯示這段日期內有複習的任務"
    assert "至少一筆複習排程" in page.date_filter_check.toolTip()


def test_val_review_001_002_today_page_action_refreshes_all_pages(qtbot, tmp_path: Path) -> None:
    """VAL-REVIEW-001/002: due metadata and completion update the whole shell."""

    window = create_test_window(qtbot, tmp_path)
    created = window.services.tasks.create(manual_draft("今日練習", datetime(2026, 1, 10, 9, 30)))
    page = window.page_widgets["reviews"]
    dashboard = window.page_widgets["dashboard"]
    assert isinstance(page, TodayTasksPage)
    assert isinstance(dashboard, DashboardPage)
    page.refresh()
    dashboard.refresh()
    assert page.table.rowCount() == 1
    assert page.table.item(0, 1).text() == "今日練習"
    action_cell = page.table.cellWidget(0, 3)
    assert action_cell is not None
    assert {button.text() for button in action_cell.findChildren(QPushButton)} >= {
        "完成",
        "推到明天",
    }
    assert not page.complete_button.isVisible()
    assert not page.postpone_button.isVisible()
    assert dashboard.metrics["due"].text() == "1"

    page.table.selectRow(0)
    qtbot.mouseClick(page.complete_button, Qt.MouseButton.LeftButton)

    assert page.table.rowCount() == 0
    assert dashboard.metrics["due"].text() == "0"
    assert window.services.tasks.details(created.task.id).task.completion_rate == 100.0


def test_val_review_011_today_item_can_open_its_management_row(qtbot, tmp_path: Path) -> None:
    """VAL-REVIEW-011: an overdue item can navigate to its owning task actions."""

    window = create_test_window(qtbot, tmp_path)
    created = window.services.tasks.create(manual_draft("前往管理", datetime(2026, 1, 9, 9, 30)))
    today_page = window.page_widgets["reviews"]
    task_page = window.page_widgets["tasks"]
    assert isinstance(today_page, TodayTasksPage)
    assert isinstance(task_page, TaskManagementPage)
    today_page.refresh()
    today_page.table.selectRow(0)

    qtbot.mouseClick(today_page.manage_button, Qt.MouseButton.LeftButton)

    assert window.navigation_buttons["tasks"].isChecked()
    selected = task_page._selected()
    assert selected is not None
    assert selected.id == created.task.id


def test_val_task_003_delete_confirmation_cancel_and_confirm(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """VAL-TASK-003: cancel keeps data and confirm performs cascade delete."""

    window = create_test_window(qtbot, tmp_path)
    created = window.services.tasks.create(manual_draft("刪除確認", datetime(2026, 1, 11, 9, 30)))
    page = window.page_widgets["tasks"]
    assert isinstance(page, TaskManagementPage)
    page.refresh()
    page.table.selectRow(0)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )
    page.delete_selected()
    assert window.services.tasks.details(created.task.id).task.name == "刪除確認"

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    page.delete_selected()
    assert page.table.rowCount() == 0


def test_val_calendar_002_004_006_selected_day_and_badge_refresh(qtbot, tmp_path: Path) -> None:
    """VAL-CALENDAR-002/004/006: day count/list refresh after review action."""

    window = create_test_window(qtbot, tmp_path)
    window.services.tasks.create(manual_draft("月曆任務", datetime(2026, 1, 10, 9, 30)))
    page = window.page_widgets["calendar"]
    assert isinstance(page, CalendarPage)
    window.show_page_by_id("calendar")
    page.calendar.setSelectedDate(QDate(2026, 1, 10))
    page.refresh()

    assert page.calendar.summaries[datetime(2026, 1, 10).date()].pending_count == 1
    assert page.table.rowCount() == 1
    assert page.table.item(0, 0).text() == "月曆任務"
    assert "已選擇 2026 年 01 月 10 日" in page.selection_feedback.text()
    assert not page.empty_label.isVisible()
    assert window.navigation_buttons["calendar"].isChecked()

    page.table.selectRow(0)
    qtbot.mouseClick(page.complete_button, Qt.MouseButton.LeftButton)

    assert page.table.rowCount() == 0
    assert page.calendar.summaries[datetime(2026, 1, 10).date()].pending_count == 0
    assert page.empty_label.isVisible()
    assert window.navigation_buttons["calendar"].isChecked()


def test_val_calendar_004_empty_date_click_has_visible_feedback(qtbot, tmp_path: Path) -> None:
    """VAL-CALENDAR-004: clicking an empty date visibly confirms the selection."""

    window = create_test_window(qtbot, tmp_path)
    window.show_page_by_id("calendar")
    page = window.page_widgets["calendar"]
    assert isinstance(page, CalendarPage)
    selected_date = QDate(2026, 2, 3)

    page.calendar.setSelectedDate(selected_date)
    page.calendar.clicked.emit(selected_date)

    assert page.day_label.text() == "2026 年 02 月 03 日 · 0 筆待複習"
    assert "右側清單已更新" in page.selection_feedback.text()
    assert page.empty_label.isVisible()
    assert window.page_stack.currentWidget() is page


def test_val_calendar_007_date_cells_have_hover_feedback(qtbot, tmp_path: Path) -> None:
    """VAL-CALENDAR-007: entering and leaving a day cell updates hover state."""

    window = create_test_window(qtbot, tmp_path)
    window.show_page_by_id("calendar")
    page = window.page_widgets["calendar"]
    assert isinstance(page, CalendarPage)
    page.calendar.setSelectedDate(QDate(2026, 1, 10))
    QApplication.processEvents()
    view = page.calendar._calendar_view
    assert view is not None

    target_date = QDate(2026, 1, 15)
    target_index = next(
        view.model().index(row, column)
        for row in range(1, view.model().rowCount())
        for column in range(view.model().columnCount())
        if view.model().index(row, column).data() == target_date.day()
    )
    assert page.calendar._date_for_index(target_index) == target_date
    qtbot.mouseMove(view.viewport(), pos=view.visualRect(target_index).center())

    assert page.calendar.hovered_date == target_date
    assert view.viewport().cursor().shape() is Qt.CursorShape.PointingHandCursor

    QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
    assert page.calendar.hovered_date is None


def test_val_calendar_008_mouse_wheel_does_not_change_month(qtbot, tmp_path: Path) -> None:
    """VAL-CALENDAR-008: wheel movement cannot accidentally switch calendar month."""

    window = create_test_window(qtbot, tmp_path)
    window.show_page_by_id("calendar")
    page = window.page_widgets["calendar"]
    assert isinstance(page, CalendarPage)
    page.calendar.setSelectedDate(QDate(2026, 1, 10))
    QApplication.processEvents()
    view = page.calendar._calendar_view
    assert view is not None
    before = (page.calendar.yearShown(), page.calendar.monthShown())
    wheel = QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )

    QApplication.sendEvent(view.viewport(), wheel)

    assert wheel.isAccepted()
    assert (page.calendar.yearShown(), page.calendar.monthShown()) == before


def test_val_asset_001_004_005_personalization_page_applies_assets(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """VAL-ASSET-001/004/005: the settings UI imports and applies fixed assets."""

    window = create_test_window(qtbot, tmp_path)
    window.show_page_by_id("settings")
    page = window.page_widgets["settings"]
    assert isinstance(page, PersonalizationPage)
    background_source = tmp_path / "ui-background.png"
    sticker_source = tmp_path / "ui-sticker.webp"
    write_gui_image(background_source, "PNG", 0xFF87B6A7)
    write_gui_image(sticker_source, "WEBP", 0xFFE8A173)

    selected_paths = iter((str(background_source), str(sticker_source)))
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (next(selected_paths), ""),
    )
    qtbot.mouseClick(page.upload_background_button, Qt.MouseButton.LeftButton)
    background_id = page.background_combo.currentData()
    assert background_id is not None
    qtbot.mouseClick(page.apply_background_button, Qt.MouseButton.LeftButton)

    assert all(item.background_path is not None for item in window.page_widgets.values())

    qtbot.mouseClick(page.upload_sticker_button, Qt.MouseButton.LeftButton)
    assert page.sticker_combo.currentData() is not None
    page.sticker_enabled_check.setChecked(True)
    qtbot.mouseClick(page.apply_sticker_button, Qt.MouseButton.LeftButton)

    assert window.sticker_label.isVisible()
    assert window.sticker_label.size().width() == 112
    assert not window.sticker_label.pixmap().isNull()
    window.hide()
    window.show()
    QApplication.processEvents()
    first_navigation_y = window.navigation_buttons["reviews"].geometry().top()
    assert window.sticker_label.y() < first_navigation_y
    assert window.sticker_label.x() >= 0
    assert (
        window.sticker_label.x() + window.sticker_label.width()
        <= window.navigation_frame.width()
    )

    page.background_mode_combo.setCurrentIndex(page.background_mode_combo.findData("page"))
    page.background_page_combo.setCurrentIndex(page.background_page_combo.findData("calendar"))
    page.background_combo.setCurrentIndex(page.background_combo.findData(background_id))
    qtbot.mouseClick(page.apply_background_button, Qt.MouseButton.LeftButton)

    assert window.page_widgets["calendar"].background_path is not None
    assert window.page_widgets["reviews"].background_path is None


def test_val_asset_gif_sticker_is_applied_and_playing(qtbot, tmp_path: Path, monkeypatch) -> None:
    """GIF stickers are accepted, applied to the left slot, and handed to QMovie."""

    window = create_test_window(qtbot, tmp_path)
    window.show_page_by_id("settings")
    page = window.page_widgets["settings"]
    assert isinstance(page, PersonalizationPage)
    sticker_source = tmp_path / "ui-sticker.gif"
    write_gui_gif(sticker_source)
    monkeypatch.setattr(
        QFileDialog,
        "getOpenFileName",
        lambda *_args, **_kwargs: (str(sticker_source), ""),
    )

    qtbot.mouseClick(page.upload_sticker_button, Qt.MouseButton.LeftButton)
    qtbot.wait(80)

    movie = window.sticker_label.movie()
    assert isinstance(movie, QMovie)
    assert movie.isValid()
    assert (
        window.sticker_label.x() + window.sticker_label.width()
        <= window.navigation_frame.width()
    )
