from __future__ import annotations

from pathlib import Path

import pytest

from task_assignment.config import APP_DATA_ENV, AppDataError, AppPaths


def test_val_app_001_override_uses_only_test_directory(tmp_path: Path) -> None:
    """VAL-APP-001: an injected data directory contains every writable path."""

    paths = AppPaths.resolve(tmp_path / "isolated")
    paths.ensure_directories()

    assert paths.base_dir == (tmp_path / "isolated").resolve()
    assert paths.database.parent == paths.base_dir
    for directory in (
        paths.assets,
        paths.backgrounds,
        paths.stickers,
        paths.backups,
        paths.logs,
    ):
        assert directory.is_dir()
        assert directory.is_relative_to(paths.base_dir)


def test_val_app_002_default_path_uses_local_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VAL-APP-002: production storage resolves below Windows Local AppData."""

    monkeypatch.delenv(APP_DATA_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    paths = AppPaths.resolve()

    assert paths.base_dir == (tmp_path / "TaskAssignment").resolve()


def test_missing_local_app_data_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APP_DATA_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(AppDataError, match="Local AppData"):
        AppPaths.resolve()


def test_existing_file_is_never_overwritten_when_directory_creation_fails(
    tmp_path: Path,
) -> None:
    """VAL-APP-004: startup path failure preserves an existing file."""

    blocked_path = tmp_path / "blocked"
    blocked_path.write_text("keep me", encoding="utf-8")
    paths = AppPaths.from_base_dir(blocked_path)

    with pytest.raises(AppDataError):
        paths.ensure_directories()

    assert blocked_path.read_text(encoding="utf-8") == "keep me"
