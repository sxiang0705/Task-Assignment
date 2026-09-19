"""Central logging configuration that avoids recording user content."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from task_assignment.config import AppPaths

LOGGER_NAME = "task_assignment"


def configure_logging(paths: AppPaths) -> logging.Logger:
    """Configure a small rotating technical log for this process."""

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = paths.logs / "task_assignment.log"
    existing_file = next(
        (
            handler
            for handler in logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and getattr(handler, "baseFilename", None) == str(log_path)
        ),
        None,
    )
    for existing_handler in tuple(logger.handlers):
        if (
            isinstance(existing_handler, RotatingFileHandler)
            and existing_handler is not existing_file
        ):
            logger.removeHandler(existing_handler)
            existing_handler.close()
    if existing_file is None:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)

    return logger
