"""Restore real backup data through the worker, then reopen in a fresh process."""

import os
import subprocess
import sys
from pathlib import Path

SCENARIO = r'''
import hashlib
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMessageBox
from task_assignment.app import create_application
from task_assignment.application import TaskDraft
from task_assignment.domain.enums import ScheduleMode, ScheduleStatus

root = Path(sys.argv[1])
stage = sys.argv[2]
now = datetime(2026, 9, 9, 12)
app, window = create_application([], data_dir=root, now_provider=lambda: now)
services = window.services
window.show()

def draft(name):
    return TaskDraft(name=name, start_at=now, schedule_mode=ScheduleMode.MANUAL,
        manual_schedule_times=(now, now + timedelta(days=1)))

if stage == "prepare":
    details = services.tasks.create(draft("備份前任務"))
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    source = root / "source.png"
    assert image.save(str(source))
    asset = services.assets.import_asset(source, "background")
    services.assets.configure_background(asset.id, mode="global")
    services.settings.set("interface_motion", "reduced")
    backup = services.backups.create_backup()
    services.tasks.update(details.task.id, draft("備份後修改"))
    schedules = services.tasks.details(details.task.id).schedules
    services.reviews.complete(schedules[0].id)
    services.tasks.create(draft("備份後新增"))
    services.settings.set("interface_motion", "off")
    # Corrupt only the isolated imported asset; restore must replace its bytes.
    asset.absolute_path.write_bytes(b"changed asset")
    assert len(services.tasks.query_page().items) == 2
    print(str(backup.path), flush=True)
    QTimer.singleShot(0, window.close)
elif stage == "restore":
    assert len(services.tasks.query_page().items) == 2
    backup = window.page_widgets["backup"]
    QMessageBox.information = lambda *args: QMessageBox.StandardButton.Ok
    errors = []
    restored = []
    backup.restart_required.connect(restored.append)
    QMessageBox.warning = lambda *args: errors.append(args)
    source = Path(sys.argv[3])

    def finish():
        if backup._thread is not None:
            QTimer.singleShot(20, finish)
            return
        assert not errors
        assert len(restored) == 1 and restored[0].restore_point.is_file()
        assert window._restart_pending
        assert not window.page_stack.isEnabled()
        assert not window.navigation_frame.isEnabled()
        assert window._operation_owner is None
        assert window.close()

    def start():
        backup._start_operation("import", lambda: services.backups.import_backup(source))
        assert not window.navigation_frame.isEnabled()
        assert not window.close()
        finish()

    QTimer.singleShot(0, start)
elif stage == "verify":
    page = services.tasks.query_page()
    assert [item.name for item in page.items] == ["備份前任務"]
    details = services.tasks.details(page.items[0].id)
    assert len(details.schedules) == 2
    assert all(item.status == ScheduleStatus.PENDING for item in details.schedules)
    assert services.settings.get("interface_motion") == "reduced"
    asset = services.assets.background_for("reviews")
    assert asset is not None and asset.available
    assert hashlib.sha256(asset.absolute_path.read_bytes()).digest() == hashlib.sha256(
        (root / "source.png").read_bytes()).digest()
    assert not window._restart_pending
    assert window.page_stack.isEnabled()
    QTimer.singleShot(0, window.close)
else:
    raise AssertionError(stage)

QTimer.singleShot(12000, lambda: app.exit(87))
code = app.exec()
assert not window.isVisible()
logging.shutdown()
sys.exit(code)
'''


def test_backup_restore_survives_real_process_restart(tmp_path: Path):
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    def run(stage, *args):
        result = subprocess.run(
            [sys.executable, "-c", SCENARIO, str(tmp_path), stage, *args],
            env=environment, capture_output=True, text=True, timeout=20, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Traceback" not in result.stderr
        return result.stdout.strip()

    archive = run("prepare")
    assert Path(archive).is_file()
    run("restore", archive)
    run("verify")
    log = (tmp_path / "logs" / "task_assignment.log").read_text(encoding="utf-8")
    assert log.count("Application shutting down") == 3
