from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from task_assignment.application import ServiceConflictError, ServiceValidationError, TaskDraft
from task_assignment.application.services import create_services
from task_assignment.config import AppPaths
from task_assignment.domain.enums import ScheduleMode
from task_assignment.infrastructure.backup import (
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    BackupArchiveStorageError,
)
from task_assignment.infrastructure.database import (
    BackupLogEntry,
    BackupLogRepository,
    Database,
    DatabaseError,
    RepositoryError,
    SettingsRepository,
)
from task_assignment.infrastructure.database.migrations import MIGRATIONS

NOW = datetime(2026, 2, 3, 14, 5, 6)


def _context(root: Path):
    paths = AppPaths.from_base_dir(root)
    paths.ensure_directories()
    database = Database(paths.database)
    database.initialize()
    return paths, database, create_services(database, paths=paths, now_provider=lambda: NOW)


def _add_task_and_asset(root: Path, name: str = "來源任務"):
    paths, database, services = _context(root)
    services.tasks.create(
        TaskDraft(
            name=name,
            start_at=datetime(2026, 2, 1, 9),
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(datetime(2026, 2, 4, 9),),
        )
    )
    source_image = root / "source.png"
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    assert image.save(str(source_image), "PNG")
    asset = services.assets.import_asset(source_image, "background")
    services.assets.configure_background(asset.id, mode="global")
    return paths, database, services, asset


def _manifest(archive_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive_path) as archive:
        return json.loads(archive.read("manifest.json"))


def _rewrite_archive(
    source: Path,
    target: Path,
    mutate: callable,
) -> None:
    with zipfile.ZipFile(source) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    rewritten = mutate(entries)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in rewritten:
            archive.writestr(name, content)


def test_backup_zip_contains_verified_snapshot_assets_and_no_sensitive_files(
    tmp_path: Path,
) -> None:
    paths, database, services, asset = _add_task_and_asset(tmp_path / "source")
    SettingsRepository(database).set("oauth_refresh_token", "never-export", now=NOW)
    BackupLogRepository(database).add(
        BackupLogEntry(operation="backup", occurred_at=NOW, success=False)
    )
    (paths.logs / "task-assignment.log").write_text("private log", encoding="utf-8")
    (paths.backups / "older.zip").write_bytes(b"older")

    result = services.backups.create_backup()
    manifest = _manifest(result.path)

    assert result.path.suffix == ".zip"
    assert manifest["format"] == BACKUP_FORMAT
    assert manifest["format_version"] == BACKUP_FORMAT_VERSION
    assert manifest["schema_version"] == MIGRATIONS[-1].version
    assert manifest["created_at"] == NOW.isoformat(timespec="seconds")
    with zipfile.ZipFile(result.path) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "database.sqlite3"} <= names
        assert {"assets/backgrounds/", "assets/stickers/"} <= names
        assert asset.relative_path in names
        assert "older.zip" not in names
        assert not any(name.endswith(".log") for name in names)
        for record in manifest["files"]:
            payload = archive.read(record["path"])
            assert record["size"] == len(payload)
            assert record["sha256"] == hashlib.sha256(payload).hexdigest()
        snapshot = tmp_path / "snapshot.sqlite3"
        snapshot.write_bytes(archive.read("database.sqlite3"))

    with Database(snapshot).connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM backup_logs WHERE success = 0").fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM settings WHERE key = 'oauth_refresh_token'"
            ).fetchone()[0]
            == 0
        )
    with database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM backup_logs WHERE operation = 'backup' AND success = 1"
            ).fetchone()[0]
            == 1
        )


def test_import_replaces_database_and_assets_but_preserves_external_directories(
    tmp_path: Path,
) -> None:
    source_paths, _, source_services, source_asset = _add_task_and_asset(tmp_path / "source")
    BackupLogRepository(source_services.backups.archive.database).add(
        BackupLogEntry(operation="gmail_send", occurred_at=NOW, success=True)
    )
    backup = source_services.backups.create_backup()

    target_paths, _, target_services, _ = _add_task_and_asset(tmp_path / "target", "即將被取代")
    existing_backup = target_paths.backups / "keep.zip"
    existing_log = target_paths.logs / "keep.log"
    existing_backup.write_bytes(b"keep backup")
    existing_log.write_text("keep log", encoding="utf-8")

    result = target_services.backups.import_backup(backup.path)

    assert result.restart_required is True
    assert result.restore_point.is_file()
    assert existing_backup.read_bytes() == b"keep backup"
    assert existing_log.read_text(encoding="utf-8") == "keep log"
    assert [item.name for item in target_services.tasks.query()] == ["來源任務"]
    restored_asset = target_services.assets.list_assets("background")[0]
    assert restored_asset.relative_path == source_asset.relative_path
    assert (
        restored_asset.absolute_path.read_bytes()
        == (source_paths.base_dir / source_asset.relative_path).read_bytes()
    )
    with target_services.backups.archive.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM backup_logs WHERE operation = 'gmail_send'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM backup_logs WHERE operation = 'import' AND success = 1"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("format", "other-product", "不是 Task Assignment"),
        ("format_version", 999, "格式版本"),
        ("schema_version", 999, "資料庫版本"),
    ),
)
def test_import_rejects_incompatible_manifest_before_restore_point(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    original = source_services.backups.create_backup().path
    invalid = tmp_path / f"invalid-{field}.zip"

    def mutate(entries):
        output = []
        for name, content in entries:
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest[field] = value
                content = json.dumps(manifest).encode()
            output.append((name, content))
        return output

    _rewrite_archive(original, invalid, mutate)
    target_paths, _, target_services, _ = _add_task_and_asset(tmp_path / "target", "保留")

    with pytest.raises(ServiceValidationError, match=message):
        target_services.backups.import_backup(invalid)

    assert [item.name for item in target_services.tasks.query()] == ["保留"]
    assert not list(target_paths.backups.glob("pre-import-restore-*.zip"))


def test_import_rejects_checksum_path_traversal_and_corrupt_database(
    tmp_path: Path,
) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    original = source_services.backups.create_backup().path
    target_paths, _, target_services, _ = _add_task_and_asset(tmp_path / "target", "保留")

    checksum_bad = tmp_path / "checksum.zip"

    def bad_checksum(entries):
        output = []
        for name, content in entries:
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest["files"][0]["sha256"] = "0" * 64
                content = json.dumps(manifest).encode()
            output.append((name, content))
        return output

    _rewrite_archive(original, checksum_bad, bad_checksum)

    traversal = tmp_path / "traversal.zip"
    _rewrite_archive(original, traversal, lambda entries: [*entries, ("../escape.txt", b"x")])

    corrupt = tmp_path / "corrupt.zip"

    def corrupt_database(entries):
        output = []
        manifest = json.loads(next(content for name, content in entries if name == "manifest.json"))
        database_payload = b"not sqlite"
        for record in manifest["files"]:
            if record["path"] == "database.sqlite3":
                record["size"] = len(database_payload)
                record["sha256"] = hashlib.sha256(database_payload).hexdigest()
        for name, content in entries:
            if name == "database.sqlite3":
                content = database_payload
            elif name == "manifest.json":
                content = json.dumps(manifest).encode()
            output.append((name, content))
        return output

    _rewrite_archive(original, corrupt, corrupt_database)

    for archive_path in (checksum_bad, traversal, corrupt):
        with pytest.raises(ServiceValidationError):
            target_services.backups.import_backup(archive_path)
        assert [item.name for item in target_services.tasks.query()] == ["保留"]
        assert not list(target_paths.backups.glob("pre-import-restore-*.zip"))


def test_install_failure_rolls_back_database_and_assets_and_keeps_restore_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    backup = source_services.backups.create_backup()
    target_paths, _, target_services, target_asset = _add_task_and_asset(
        tmp_path / "target", "原始任務"
    )
    original_asset_bytes = target_asset.absolute_path.read_bytes()

    def fail_asset_move(_source: Path, _destination: Path) -> None:
        raise BackupArchiveStorageError("模擬素材取代失敗。")

    monkeypatch.setattr(target_services.backups.archive, "_move_assets", fail_asset_move)

    with pytest.raises(ServiceConflictError, match="模擬素材取代失敗"):
        target_services.backups.import_backup(backup.path)

    assert [item.name for item in target_services.tasks.query()] == ["原始任務"]
    assert target_asset.absolute_path.read_bytes() == original_asset_bytes
    assert list(target_paths.backups.glob("pre-import-restore-*.zip"))
    with target_services.backups.archive.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM backup_logs WHERE operation = 'import' AND success = 0"
            ).fetchone()[0]
            == 1
        )


def test_backup_failure_is_logged_and_leaves_no_partial_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, database, services = _context(tmp_path / "target")

    def fail_create(_destination: Path) -> None:
        raise DatabaseError("模擬 SQLite snapshot 失敗。")

    monkeypatch.setattr(database, "backup_to", fail_create)

    with pytest.raises(ServiceConflictError, match="無法建立完整的備份 ZIP"):
        services.backups.create_backup()

    assert not list(paths.backups.iterdir())
    with database.connection() as connection:
        row = connection.execute(
            "SELECT success, error_message FROM backup_logs WHERE operation = 'backup'"
        ).fetchone()
    assert tuple(row) == (0, "無法建立完整的備份 ZIP。")


def test_prepare_import_failure_does_not_delete_unmoved_current_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    backup = source_services.backups.create_backup()
    target_paths, database, target_services, target_asset = _add_task_and_asset(
        tmp_path / "target", "不可刪除"
    )
    original_asset = target_asset.absolute_path.read_bytes()

    def fail_checkpoint() -> None:
        raise DatabaseError("模擬 checkpoint 失敗。")

    monkeypatch.setattr(database, "checkpoint", fail_checkpoint)

    with pytest.raises(ServiceConflictError):
        target_services.backups.import_backup(backup.path)

    assert target_paths.database.is_file()
    assert [item.name for item in target_services.tasks.query()] == ["不可刪除"]
    assert target_asset.absolute_path.read_bytes() == original_asset


def test_import_still_requires_restart_if_success_log_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    backup = source_services.backups.create_backup()
    _, _, target_services, _ = _add_task_and_asset(tmp_path / "target", "舊任務")
    original_add = target_services.backups.logs.add
    calls = 0

    def fail_second_log(entry: BackupLogEntry) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RepositoryError("模擬成功紀錄失敗。")
        return original_add(entry)

    monkeypatch.setattr(target_services.backups.logs, "add", fail_second_log)

    result = target_services.backups.import_backup(backup.path)

    assert result.restart_required is True
    assert result.warning == "備份操作結果無法寫入紀錄。"
    assert [item.name for item in target_services.tasks.query()] == ["來源任務"]


@pytest.mark.parametrize(
    "unsafe_name",
    (
        "../escape.txt",
        "/absolute.txt",
        "C:/windows.txt",
        "assets\\backgrounds/mixed.png",
        "assets/backgrounds/unlisted.png",
    ),
)
def test_import_rejects_unsafe_or_unlisted_entries_without_writing_formal_data(
    tmp_path: Path, unsafe_name: str
) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    original = source_services.backups.create_backup().path
    invalid = tmp_path / "unsafe.zip"
    _rewrite_archive(original, invalid, lambda entries: [*entries, (unsafe_name, b"payload")])
    target_paths, _, target_services, target_asset = _add_task_and_asset(
        tmp_path / "target", "保留"
    )
    original_assets = {item.name for item in target_paths.backgrounds.iterdir()}

    with pytest.raises(ServiceValidationError):
        target_services.backups.import_backup(invalid)

    assert [item.name for item in target_services.tasks.query()] == ["保留"]
    assert {item.name for item in target_paths.backgrounds.iterdir()} == original_assets
    assert target_asset.absolute_path.is_file()
    assert not list(target_paths.backups.glob("pre-import-restore-*.zip"))
    assert not (tmp_path / "escape.txt").exists()


def test_import_rejects_duplicate_zip_and_manifest_paths(tmp_path: Path) -> None:
    _, _, source_services, _ = _add_task_and_asset(tmp_path / "source")
    original = source_services.backups.create_backup().path
    with zipfile.ZipFile(original) as archive:
        database_payload = archive.read("database.sqlite3")

    duplicate_zip = tmp_path / "duplicate-zip.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        _rewrite_archive(
            original,
            duplicate_zip,
            lambda entries: [*entries, ("database.sqlite3", database_payload)],
        )

    duplicate_manifest = tmp_path / "duplicate-manifest.zip"

    def duplicate_record(entries):
        output = []
        for name, content in entries:
            if name == "manifest.json":
                manifest = json.loads(content)
                manifest["files"].append(dict(manifest["files"][0]))
                content = json.dumps(manifest).encode()
            output.append((name, content))
        return output

    _rewrite_archive(original, duplicate_manifest, duplicate_record)
    _, _, target_services = _context(tmp_path / "target")

    for invalid in (duplicate_zip, duplicate_manifest):
        with pytest.raises(ServiceValidationError, match="重複路徑"):
            target_services.backups.import_backup(invalid)
