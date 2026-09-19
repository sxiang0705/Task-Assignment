"""Earliest custom PyInstaller timestamp; no filesystem or account access."""

import sys
from time import perf_counter

sys._task_assignment_runtime_started = perf_counter()
