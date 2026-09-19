"""Measure launch-to-ready on isolated scale data; not a cold-OS benchmark."""

import argparse
import json
import os
import runpy
import subprocess
import time
from pathlib import Path

from task_assignment.infrastructure.database import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                "tests" / "performance" / "test_scale.py"))
    results = []
    for index in range(3):
        data = output / f"run-{index + 1}"
        data.mkdir()
        database = Database(data / "task_assignment.db")
        database.initialize()
        fixture["_seed_scale_data"](database)
        environment = dict(os.environ, TASK_ASSIGNMENT_DATA_DIR=str(data))
        environment.pop("QT_QPA_PLATFORM", None)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        started = time.perf_counter()
        process = subprocess.Popen([str(exe)], env=environment, startupinfo=startup)
        ready = False
        payload = ""
        try:
            while time.perf_counter() - started < 30:
                log = data / "logs" / "task_assignment.log"
                if log.exists():
                    payload = log.read_text(encoding="utf-8")
                if "Application ready" in payload:
                    ready = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            elapsed = time.perf_counter() - started
        finally:
            # Only this tool's isolated child is stopped; this does not test graceful close.
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        results.append({
            "run": index + 1, "ready": ready, "launch_to_ready_seconds": round(elapsed, 3),
            "phases": [line for line in payload.splitlines() if "Startup " in line],
        })
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    report = {
        "scope": "hidden Windows, 5000 tasks/50000 schedules; OS cache not controlled",
        "graceful_close_tested": False, "runs": results,
    }
    (output / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if all(result["ready"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
