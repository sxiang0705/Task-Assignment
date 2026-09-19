import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("scale", [1.5, 2.0])
def test_process_scale_keeps_primary_controls_in_bounds(tmp_path, scale):
    script = Path(__file__).resolve().parents[2] / "tools/verify_scaling.py"
    output = tmp_path / "verification"
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(output)],
        env=dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_SCALE_FACTOR=str(scale)),
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert report["scale"] == pytest.approx(scale)
    assert len(report["pages"]) == 6
