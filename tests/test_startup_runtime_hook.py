import runpy
import sys
from pathlib import Path
from time import perf_counter

from task_assignment.app import create_application


def test_runtime_timestamp_is_captured_and_reported(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_task_assignment_runtime_started", 0.0, raising=False)
    before = perf_counter()
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/startup_runtime_hook.py"))
    assert before <= sys._task_assignment_runtime_started <= perf_counter()
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    log = (tmp_path / "logs/task_assignment.log").read_text(encoding="utf-8")
    assert "Startup runtime to application ms:" in log
