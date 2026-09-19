"""Task Assignment process entry point."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from time import perf_counter

from task_assignment.application.errors import ServiceError
from task_assignment.config import AppDataError, AppPaths
from task_assignment.infrastructure.database.connection import DatabaseError
from task_assignment.infrastructure.database.migrations import DatabaseMigrationError
from task_assignment.version import APP_VERSION


def create_application(
    argv: Sequence[str] | None = None,
    *,
    data_dir: str | Path | None = None,
    now_provider: Callable[[], datetime] = datetime.now,
):
    """Create the Qt application and main window using injectable storage."""

    started = perf_counter()
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from task_assignment.application.services import create_services
    from task_assignment.infrastructure.database.connection import Database
    from task_assignment.logging_config import configure_logging
    from task_assignment.ui.app_shell import AppShell

    imports_finished = perf_counter()
    paths = AppPaths.resolve(data_dir)
    paths.ensure_directories()
    logger = configure_logging(paths)
    logger.info("Application starting")
    runtime_started = getattr(sys, "_task_assignment_runtime_started", None)
    if runtime_started is not None:
        logger.info(
            "Startup runtime to application ms: %.1f", (started - runtime_started) * 1000
        )
    database = Database(paths.database)
    database.initialize()
    services = create_services(database, now_provider=now_provider, paths=paths)
    storage_finished = perf_counter()
    services.tasks.refresh_all_statuses()
    statuses_finished = perf_counter()

    application = QApplication.instance() or QApplication(list(argv or []))
    application.setApplicationName("Task Assignment")
    application.setOrganizationName("Task Assignment")
    application.setApplicationVersion(APP_VERSION)
    icon_path = _resource_path("resources/icons/task_assignment.ico")
    if not icon_path.is_file():
        icon_path = _resource_path("resources/icons/task_assignment.svg")
    if icon_path.is_file():
        application.setWindowIcon(QIcon(str(icon_path)))

    qt_finished = perf_counter()
    window = AppShell(paths=paths, services=services)
    shell_finished = perf_counter()
    logger.info(
        "Startup phases ms: imports=%.1f storage=%.1f statuses=%.1f qt=%.1f shell=%.1f total=%.1f",
        (imports_finished - started) * 1000,
        (storage_finished - imports_finished) * 1000,
        (statuses_finished - storage_finished) * 1000,
        (qt_finished - statuses_finished) * 1000,
        (shell_finished - qt_finished) * 1000,
        (shell_finished - started) * 1000,
    )
    application.aboutToQuit.connect(lambda: logger.info("Application shutting down"))
    return application, window


def _resource_path(relative_path: str) -> Path:
    packaged_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return packaged_root / relative_path


def main(argv: Sequence[str] | None = None) -> int:
    """Run the desktop application with a user-facing startup failure."""

    arguments = list(argv if argv is not None else sys.argv)
    try:
        application, window = create_application(arguments)
    except (AppDataError, DatabaseError, DatabaseMigrationError, ServiceError) as exc:
        _show_startup_error(str(exc), arguments)
        return 1
    except Exception:
        logging.getLogger("task_assignment").exception("Unexpected startup failure")
        _show_startup_error("程式無法啟動，請查看應用程式日誌。", arguments)
        return 1

    first_frame_started = perf_counter()
    window.show()
    application.processEvents()
    logging.getLogger("task_assignment").info(
        "Startup first frame ms: %.1f", (perf_counter() - first_frame_started) * 1000
    )
    logging.getLogger("task_assignment").info("Application ready")
    return application.exec()


def _show_startup_error(message: str, argv: Sequence[str]) -> None:
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        application = QApplication.instance() or QApplication(list(argv))
        QMessageBox.critical(None, "Task Assignment 啟動失敗", message)
        application.processEvents()
    except Exception:
        print(f"Task Assignment 啟動失敗：{message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
