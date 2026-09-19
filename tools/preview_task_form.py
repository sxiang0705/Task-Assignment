"""Render revised task forms with an explicitly loaded local CJK font."""

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QDateTime
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from task_assignment.app import create_application
from task_assignment.domain.enums import ScheduleMode
from task_assignment.ui.task_dialog import TaskDialog


def main():
    app = QApplication.instance() or QApplication([])
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msjh.ttc"
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id < 0:
        raise RuntimeError("Preview requires the installed Microsoft JhengHei font")
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    output = Path(__file__).resolve().parents[1] / "artifacts/verification/form-20260910"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ta-form-preview-") as data:
        _, window = create_application([], data_dir=data)
        window.services.tasks.create_category("程式學習")
        window.services.tasks.create_tag("考試重點")
        dialog = TaskDialog(window.services.tasks, parent=window)
        dialog.name_edit.setText("Python 學習筆記")
        dialog.start_edit.setDateTime(QDateTime(datetime(2026, 9, 10, 19, 30)))
        dialog.resize(1020, 780)
        dialog.show()
        app.processEvents()
        assert dialog.scroll.verticalScrollBar().maximum() == 0
        dialog.grab().save(str(output / "curve.png"))
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(ScheduleMode.MANUAL.value))
        for day in (11, 14, 20):
            dialog.manual_picker.setDateTime(QDateTime(datetime(2026, 9, day, 19, 30)))
            dialog.manual_add_button.click()
        app.processEvents()
        assert dialog.scroll.verticalScrollBar().maximum() == 0
        dialog.grab().save(str(output / "manual.png"))
        dialog.close()
        window.close()
        app.processEvents()
        logging.shutdown()
    print(output)


if __name__ == "__main__":
    main()
