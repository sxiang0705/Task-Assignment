from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, Qt
from PySide6.QtWidgets import QInputDialog

from task_assignment.app import create_application
from task_assignment.domain.enums import ScheduleMode
from task_assignment.ui.task_dialog import TaskDialog


def test_catalog_can_be_created_and_selected_in_form(qtbot, tmp_path, monkeypatch):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    dialog = TaskDialog(window.services.tasks, parent=window)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("考試準備", True))
    dialog.add_category_button.click()
    dialog.add_tag_button.click()
    draft = dialog.draft()
    assert draft.category_id == window.services.tasks.categories()[0].id
    assert draft.tag_ids == (window.services.tasks.tags()[0].id,)


def test_tag_checkboxes_preserve_multiple_tags_and_allow_removal(qtbot, tmp_path):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    service = window.services.tasks
    for name in ("考試", "重點", "筆記"):
        service.create_tag(name)
    dialog = TaskDialog(service, parent=window)
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("標籤複選")
    assert dialog.draft().tag_ids == ()
    for index in (0, 1):
        item = dialog.tag_list.item(index)
        assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
        item.setCheckState(Qt.CheckState.Checked)
    expected = dialog.draft().tag_ids
    assert len(expected) == 2
    details = service.create(dialog.draft())
    editing = TaskDialog(service, details=details, parent=window)
    qtbot.addWidget(editing)
    assert editing.draft().tag_ids == expected
    editing.tag_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    updated = service.update(details.task.id, editing.draft())
    assert {tag.id for tag in updated.task.tags} == {expected[1]}
    editing.tag_list.item(1).setCheckState(Qt.CheckState.Unchecked)
    updated = service.update(details.task.id, editing.draft())
    assert updated.task.tags == ()


def test_manual_picker_visibility_duplicates_and_count_limits(qtbot, tmp_path):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    dialog = TaskDialog(window.services.tasks, parent=window)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_edit.setText("自訂日期")
    assert dialog.manual_panel.isHidden()
    assert not dialog.less_count_button.isEnabled()
    dialog.review_count_spin.setValue(10)
    assert not dialog.more_count_button.isEnabled()
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(ScheduleMode.MANUAL.value))
    assert not dialog.manual_panel.isHidden()
    assert dialog.count_panel.isHidden()
    assert not dialog.manual_remove_button.isEnabled()
    dialog.manual_add_button.click()
    assert dialog.manual_list.count() == 1
    assert not dialog.manual_add_button.isEnabled()
    dialog.manual_list.setCurrentRow(0)
    dialog.manual_remove_button.click()
    assert not dialog.draft().manual_schedule_times
    assert dialog.manual_add_button.isEnabled()


def test_manual_dates_survive_save_edit_and_mode_toggle(qtbot, tmp_path):
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    dialog = TaskDialog(window.services.tasks, parent=window)
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("保存日期")
    dialog.start_edit.setDateTime(QDateTime(datetime(2026, 9, 10, 9)))
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(ScheduleMode.MANUAL.value))
    for day in (15, 12):
        dialog.manual_picker.setDateTime(QDateTime(datetime(2026, 9, day, 9)))
        dialog.manual_add_button.click()
    expected = dialog.draft().manual_schedule_times
    details = window.services.tasks.create(dialog.draft())
    editing = TaskDialog(window.services.tasks, details=details, parent=window)
    qtbot.addWidget(editing)
    assert editing.draft().manual_schedule_times == expected
    editing.mode_combo.setCurrentIndex(0)
    editing.mode_combo.setCurrentIndex(editing.mode_combo.findData(ScheduleMode.MANUAL.value))
    assert editing.draft().manual_schedule_times == expected


def test_regular_form_keeps_preview_outside_scroll(qtbot, tmp_path):
    app, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    dialog = TaskDialog(window.services.tasks, parent=window)
    qtbot.addWidget(dialog)
    dialog.resize(1020, 780)
    dialog.show()
    app.processEvents()
    assert not dialog.scroll.isAncestorOf(dialog.preview_list)
    assert dialog.preview_list.isVisible()
    assert dialog.scroll.verticalScrollBar().maximum() == 0
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(ScheduleMode.MANUAL.value))
    app.processEvents()
    assert dialog.scroll.verticalScrollBar().maximum() == 0


def test_calendar_selection_has_no_delayed_reveal(qtbot, tmp_path, monkeypatch):
    now = datetime(2026, 9, 10, 12)
    _, window = create_application([], data_dir=tmp_path, now_provider=lambda: now)
    qtbot.addWidget(window)
    window.show()
    window.show_page_by_id("calendar")
    page = window.page_widgets["calendar"]
    # QCalendarWidget starts on the OS date, which may equal the target date.
    page.calendar.setSelectedDate(QDate(2026, 9, 10))
    calls = []
    original = window.services.reviews.day_page

    def tracked(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(window.services.reviews, "day_page", tracked)
    page.calendar.setSelectedDate(QDate(2026, 9, 12))
    assert calls == [QDate(2026, 9, 12).toPython()]
    assert "12" in page.day_label.text()
    assert getattr(page.table, "_reveal_motion", None) is None
