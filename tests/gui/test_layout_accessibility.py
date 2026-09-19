from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialogButtonBox

from task_assignment.app import create_application
from task_assignment.ui.pages import PersonalizationPage
from task_assignment.ui.task_dialog import TaskDialog


@pytest.mark.parametrize("width,height", ((720, 480), (800, 560), (1280, 720), (1920, 1080)))
def test_val_gui_003_004_primary_pages_fit_or_scroll(
    qtbot,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    """VAL-GUI-003/004: primary pages remain reachable across the size matrix."""

    application, window = create_application([], data_dir=tmp_path / f"layout-{width}x{height}")
    qtbot.addWidget(window)
    window.resize(width, height)
    window.show()
    application.processEvents()

    assert window.width() >= min(width, window.minimumWidth())
    assert window.height() >= min(height, window.minimumHeight())
    for page_id, page in window.page_widgets.items():
        window.show_page_by_id(page_id)
        application.processEvents()
        assert page.help_button.isVisible()
        assert page.help_button.width() > 0
        assert page.help_button.height() > 0
        if isinstance(page, PersonalizationPage):
            assert page.scroll.verticalScrollBar().maximum() >= 0


def test_val_gui_003_task_dialog_scrolls_while_actions_remain_visible(
    qtbot,
    tmp_path: Path,
) -> None:
    """The densest form remains usable in a 520x420 logical-pixel window."""

    application, window = create_application([], data_dir=tmp_path / "dialog-layout")
    qtbot.addWidget(window)
    dialog = TaskDialog(window.services.tasks, parent=window)
    qtbot.addWidget(dialog)
    dialog.resize(520, 420)
    dialog.show()
    application.processEvents()

    assert dialog.scroll.isVisible()
    assert dialog.scroll.verticalScrollBar().maximum() > 0
    assert dialog.button_box.isVisible()
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Save).isVisible()
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel).isVisible()


def test_val_gui_005_navigation_and_help_are_in_keyboard_focus_chain(
    qtbot,
    tmp_path: Path,
) -> None:
    """VAL-GUI-005: navigation and current-page help are keyboard reachable."""

    application, window = create_application([], data_dir=tmp_path / "focus-layout")
    qtbot.addWidget(window)
    window.show()
    application.processEvents()
    first = window.navigation_buttons["reviews"]
    first.setFocus()
    focused = {first}
    current = first
    for _ in range(2_000):
        current = current.nextInFocusChain()
        focused.add(current)
        if current is first:
            break

    assert set(window.navigation_buttons.values()).issubset(focused)
    assert window.page_widgets["reviews"].help_button in focused
