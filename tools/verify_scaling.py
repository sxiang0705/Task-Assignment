"""Check logical bounds under a process-local Qt scale factor; not native DPI certification."""

import argparse
import json
import logging
from pathlib import Path

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QAbstractButton, QAbstractScrollArea, QDialogButtonBox

from task_assignment.app import create_application
from task_assignment.ui.task_dialog import TaskDialog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    app, window = create_application([], data_dir=args.output / "data")
    window.services.settings.set("interface_motion", "off")
    window.apply_appearance()
    window.resize(800, 560)
    window.show()
    report = {
        "scale": window.devicePixelRatioF(), "pages": [],
        "font_family_count": len(QFontDatabase.families()),
        "scope": "geometry only; native font rendering and DPI remain unverified",
    }
    try:
        for page_id, page in window.page_widgets.items():
            window.show_page_by_id(page_id)
            app.processEvents()
            checked = 0
            for button in page.findChildren(QAbstractButton):
                if not button.isVisible():
                    continue
                ancestor = button.parentWidget()
                in_scroll = False
                while ancestor is not None and ancestor is not page:
                    if isinstance(ancestor, QAbstractScrollArea):
                        in_scroll = True
                        break
                    ancestor = ancestor.parentWidget()
                if in_scroll:
                    continue
                bounds = QRect(button.mapTo(page, QPoint(0, 0)), button.size())
                assert page.rect().contains(bounds), (page_id, button.text(), bounds)
                checked += 1
            assert page.help_button.isVisible()
            assert checked > 0
            assert window.grab().save(str(args.output / f"{page_id}.png"))
            report["pages"].append({"id": page_id, "bounded_buttons": checked})
        dialog = TaskDialog(window.services.tasks, parent=window)
        dialog.resize(520, 420)
        dialog.show()
        app.processEvents()
        assert dialog.scroll.verticalScrollBar().maximum() > 0
        for standard in (
            QDialogButtonBox.StandardButton.Save, QDialogButtonBox.StandardButton.Cancel,
        ):
            button = dialog.button_box.button(standard)
            assert button.isVisible()
            assert dialog.rect().contains(QRect(button.mapTo(dialog, QPoint()), button.size()))
        assert dialog.grab().save(str(args.output / "task-dialog.png"))
        dialog.close()
        (args.output / "results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        window.close()
        app.processEvents()
        logging.shutdown()


if __name__ == "__main__":
    main()
