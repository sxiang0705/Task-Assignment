"""Application configuration and writable path resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DATA_ENV = "TASK_ASSIGNMENT_DATA_DIR"
APP_DIRECTORY_NAME = "TaskAssignment"


class AppDataError(RuntimeError):
    """Raised when the application data directory cannot be prepared safely."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    """All writable locations used by the application.

    Tests and packaged smoke checks can inject ``base_dir`` so they never touch
    a real user's Local AppData directory.
    """

    base_dir: Path
    database: Path
    assets: Path
    backgrounds: Path
    stickers: Path
    backups: Path
    logs: Path

    @classmethod
    def from_base_dir(cls, base_dir: str | Path) -> AppPaths:
        base = Path(base_dir).expanduser().resolve()
        assets = base / "assets"
        return cls(
            base_dir=base,
            database=base / "task_assignment.db",
            assets=assets,
            backgrounds=assets / "backgrounds",
            stickers=assets / "stickers",
            backups=base / "backups",
            logs=base / "logs",
        )

    @classmethod
    def resolve(cls, override: str | Path | None = None) -> AppPaths:
        """Resolve paths without depending on the process working directory."""

        if override is not None:
            return cls.from_base_dir(override)

        environment_override = os.environ.get(APP_DATA_ENV)
        if environment_override:
            return cls.from_base_dir(environment_override)

        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise AppDataError(
                "找不到 Windows Local AppData。請設定 LOCALAPPDATA，或以測試資料目錄啟動。"
            )
        return cls.from_base_dir(Path(local_app_data) / APP_DIRECTORY_NAME)

    def ensure_directories(self) -> None:
        """Create the required directory tree or fail without deleting data."""

        try:
            for directory in (
                self.base_dir,
                self.assets,
                self.backgrounds,
                self.stickers,
                self.backups,
                self.logs,
            ):
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppDataError(f"無法建立應用程式資料目錄：{self.base_dir}") from exc
