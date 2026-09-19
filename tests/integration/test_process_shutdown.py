"""Exercise the real Qt event loop in a separate process, not pytest's QApplication."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["idle", "background", "restart"])
def test_close_exits_application_process(tmp_path: Path, scenario: str) -> None:
    script = r'''
import logging
import sys
from threading import Event
from PySide6.QtCore import QTimer
from task_assignment.app import create_application

app, window = create_application([], data_dir=sys.argv[1])
window.show()
scenario = sys.argv[2]
release = Event()
backup = window.page_widgets["backup"]
exit_observed = []
app.aboutToQuit.connect(lambda: exit_observed.append(True))

def close_when_idle():
    if backup._thread is not None:
        QTimer.singleShot(20, close_when_idle)
        return
    assert window.close()
    assert not window.day_timer.isActive()

def start():
    if scenario == "background":
        backup._start_operation("backup", lambda: release.wait(3))
        assert not window.close()
        assert window.isVisible()
        release.set()
        close_when_idle()
    else:
        if scenario == "restart":
            window._lock_for_restart(None)
        close_when_idle()

QTimer.singleShot(0, start)
# A stuck event loop exits nonzero, instead of silently passing a hidden window test.
QTimer.singleShot(8000, lambda: app.exit(87))
code = app.exec()
assert exit_observed
assert not window.isVisible()
assert backup._thread is None
logging.shutdown()
sys.exit(code)
'''
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), scenario],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    log = (tmp_path / "logs" / "task_assignment.log").read_text(encoding="utf-8")
    assert "Application shutting down" in log
