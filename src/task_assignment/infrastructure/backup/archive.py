"""Create, validate, and atomically install Task Assignment backup archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from PySide6.QtGui import QImageReader

from task_assignment import __version__
from task_assignment.config import AppPaths
from task_assignment.infrastructure.database.connection import Database, DatabaseError
from task_assignment.infrastructure.database.migrations import MIGRATIONS

BACKUP_FORMAT = "task-assignment-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_ARCHIVE_PATH = "database.sqlite3"
MANIFEST_ARCHIVE_PATH = "manifest.json"
BACKGROUND_DIRECTORY = "assets/backgrounds/"
STICKER_DIRECTORY = "assets/stickers/"
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ASSET_BYTES = 20 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_SETTING_MARKERS = ("token", "oauth", "credential", "password", "secret")
SUPPORTED_ASSET_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
}
REQUIRED_DATABASE_TABLES = {
    "assets",
    "backup_logs",
    "categories",
    "review_records",
    "review_schedules",
    "schema_migrations",
    "settings",
    "tags",
    "task_tags",
    "tasks",
}


class BackupArchiveError(RuntimeError):
    """Base class for archive failures."""


class BackupArchiveValidationError(BackupArchiveError):
    """Raised when an archive is incomplete, unsafe, or incompatible."""


class BackupArchiveStorageError(BackupArchiveError):
    """Raised when a valid backup cannot be written or installed."""


@dataclass(frozen=True, slots=True)
class ArchiveFileRecord:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupArchiveInfo:
    path: Path
    created_at: datetime
    schema_version: int
    files: tuple[ArchiveFileRecord, ...]
    archive_sha256: str


@dataclass(frozen=True, slots=True)
class ValidatedBackup:
    path: Path
    created_at: datetime
    schema_version: int
    app_version: str
    files: tuple[ArchiveFileRecord, ...]
    archive_sha256: str


class BackupArchive:
    def __init__(self, database: Database, paths: AppPaths) -> None:
        self.database = database
        self.paths = paths

    def create(
        self,
        *,
        now: datetime | None = None,
        filename_prefix: str = "task-assignment-backup",
    ) -> BackupArchiveInfo:
        created_at = now or datetime.now()
        self.paths.backups.mkdir(parents=True, exist_ok=True)
        destination = self._unique_destination(filename_prefix, created_at)
        partial = destination.with_name(f".{destination.stem}.{uuid4().hex}.partial.zip")
        try:
            with tempfile.TemporaryDirectory(prefix="task-assignment-backup-") as directory:
                workspace = Path(directory)
                database_snapshot = workspace / DATABASE_ARCHIVE_PATH
                self.database.backup_to(database_snapshot)
                _remove_sensitive_settings(database_snapshot)
                payload_paths = self._collect_payload(workspace, database_snapshot)
                records = tuple(
                    ArchiveFileRecord(
                        path=archive_path,
                        size=source.stat().st_size,
                        sha256=_sha256_file(source),
                    )
                    for archive_path, source in sorted(payload_paths.items())
                )
                manifest = {
                    "format": BACKUP_FORMAT,
                    "format_version": BACKUP_FORMAT_VERSION,
                    "app_version": __version__,
                    "schema_version": MIGRATIONS[-1].version,
                    "created_at": created_at.isoformat(timespec="seconds"),
                    "files": [
                        {"path": item.path, "size": item.size, "sha256": item.sha256}
                        for item in records
                    ],
                }
                with zipfile.ZipFile(
                    partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
                ) as archive:
                    archive.writestr(BACKGROUND_DIRECTORY, b"")
                    archive.writestr(STICKER_DIRECTORY, b"")
                    archive.writestr(
                        MANIFEST_ARCHIVE_PATH,
                        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
                    )
                    for archive_path, source in sorted(payload_paths.items()):
                        archive.write(source, archive_path)
                self.validate(partial)
                os.replace(partial, destination)
        except BackupArchiveError:
            partial.unlink(missing_ok=True)
            raise
        except (DatabaseError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
            partial.unlink(missing_ok=True)
            raise BackupArchiveStorageError("無法建立完整的備份 ZIP。") from exc
        return BackupArchiveInfo(
            path=destination,
            created_at=created_at,
            schema_version=MIGRATIONS[-1].version,
            files=records,
            archive_sha256=_sha256_file(destination),
        )

    def validate(self, archive_path: str | Path) -> ValidatedBackup:
        path = Path(archive_path)
        if path.suffix.lower() != ".zip" or not path.is_file():
            raise BackupArchiveValidationError("請選擇 Task Assignment 的 ZIP 備份檔。")
        try:
            with zipfile.ZipFile(path, "r") as archive:
                normalized_infos = self._validate_entry_names(archive.infolist())
                manifest_info = normalized_infos.get(MANIFEST_ARCHIVE_PATH)
                if manifest_info is None or manifest_info.is_dir():
                    raise BackupArchiveValidationError("備份缺少 manifest.json。")
                if manifest_info.file_size > MAX_MANIFEST_BYTES:
                    raise BackupArchiveValidationError("備份 manifest 過大。")
                manifest = _load_manifest(archive.read(manifest_info))
                validated = _parse_manifest(path, manifest)
                payload_infos = {
                    name: info
                    for name, info in normalized_infos.items()
                    if not info.is_dir() and name != MANIFEST_ARCHIVE_PATH
                }
                expected_paths = {item.path for item in validated.files}
                if set(payload_infos) != expected_paths:
                    raise BackupArchiveValidationError(
                        "備份含有 manifest 未列出的檔案，或缺少已列出的檔案。"
                    )
                record_map = {item.path: item for item in validated.files}
                for name, info in payload_infos.items():
                    record = record_map[name]
                    if info.file_size != record.size:
                        raise BackupArchiveValidationError(f"備份檔案大小不符：{name}")
                    digest, size = _hash_zip_entry(archive, info)
                    if size != record.size or digest != record.sha256:
                        raise BackupArchiveValidationError(f"備份 checksum 不符：{name}")
                if DATABASE_ARCHIVE_PATH not in expected_paths:
                    raise BackupArchiveValidationError("備份缺少 database.sqlite3。")
                if BACKGROUND_DIRECTORY not in normalized_infos:
                    raise BackupArchiveValidationError("備份缺少背景素材目錄。")
                if STICKER_DIRECTORY not in normalized_infos:
                    raise BackupArchiveValidationError("備份缺少貼圖素材目錄。")
                with tempfile.TemporaryDirectory(prefix="task-assignment-validate-") as directory:
                    database_path = Path(directory) / DATABASE_ARCHIVE_PATH
                    with archive.open(payload_infos[DATABASE_ARCHIVE_PATH], "r") as source:
                        with database_path.open("wb") as target:
                            shutil.copyfileobj(source, target)
                    _validate_snapshot_database(database_path, expected_paths)
                return validated
        except BackupArchiveError:
            raise
        except (
            OSError,
            RuntimeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ) as exc:
            raise BackupArchiveValidationError(
                "ZIP 損壞或不是有效的 Task Assignment 備份。"
            ) from exc

    def install(self, archive_path: str | Path) -> ValidatedBackup:
        validated = self.validate(archive_path)
        stage_parent = self.paths.base_dir.parent
        try:
            with tempfile.TemporaryDirectory(
                prefix=".task-assignment-import-", dir=stage_parent
            ) as directory:
                extracted = Path(directory) / "validated"
                self._extract_validated(validated, extracted)
                self._install_extracted(extracted)
        except BackupArchiveError:
            raise
        except OSError as exc:
            raise BackupArchiveStorageError("無法完整取代目前資料，原資料已嘗試恢復。") from exc
        return validated

    def _collect_payload(self, workspace: Path, database_snapshot: Path) -> dict[str, Path]:
        payload = {DATABASE_ARCHIVE_PATH: database_snapshot}
        for source_root, archive_root in (
            (self.paths.backgrounds, "assets/backgrounds"),
            (self.paths.stickers, "assets/stickers"),
        ):
            if not source_root.exists():
                continue
            for source in sorted(source_root.rglob("*")):
                if not source.is_file() or source.name.startswith("."):
                    continue
                resolved = source.resolve()
                if source.is_symlink() or not resolved.is_relative_to(source_root.resolve()):
                    raise BackupArchiveStorageError("素材目錄包含不安全的連結。")
                relative = source.relative_to(source_root).as_posix()
                archive_path = f"{archive_root}/{relative}"
                target = workspace / Path(*PurePosixPath(archive_path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                payload[archive_path] = target
        _validate_snapshot_database(database_snapshot, set(payload))
        return payload

    def _validate_entry_names(self, infos: list[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
        normalized: dict[str, zipfile.ZipInfo] = {}
        casefolded: set[str] = set()
        total_size = 0
        for info in infos:
            name = _safe_archive_path(info.filename, allow_directory=info.is_dir())
            folded = _path_key(name)
            if folded in casefolded:
                raise BackupArchiveValidationError(f"備份含有重複路徑：{name}")
            casefolded.add(folded)
            if not _is_allowed_archive_path(name, info.is_dir()):
                raise BackupArchiveValidationError(f"備份包含不允許的檔案：{name}")
            if info.is_dir() and info.file_size != 0:
                raise BackupArchiveValidationError("備份素材目錄不可包含直接資料。")
            total_size += info.file_size
            if total_size > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise BackupArchiveValidationError("備份解壓縮後的大小超過安全限制。")
            if info.compress_size > 0 and info.file_size / info.compress_size > 10_000:
                raise BackupArchiveValidationError("備份包含異常壓縮比例的檔案。")
            if (
                not info.is_dir()
                and name.startswith("assets/")
                and info.file_size > MAX_ASSET_BYTES
            ):
                raise BackupArchiveValidationError("備份內的單一素材不可超過 20 MB。")
            normalized[name] = info
        return normalized

    def _extract_validated(self, validated: ValidatedBackup, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "assets" / "backgrounds").mkdir(parents=True)
        (destination / "assets" / "stickers").mkdir(parents=True)
        record_map = {item.path: item for item in validated.files}
        try:
            with zipfile.ZipFile(validated.path, "r") as archive:
                info_map = self._validate_entry_names(archive.infolist())
                for name, record in record_map.items():
                    info = info_map.get(name)
                    if info is None or info.is_dir():
                        raise BackupArchiveValidationError(f"備份缺少檔案：{name}")
                    target = destination / Path(*PurePosixPath(name).parts)
                    resolved = target.resolve()
                    if not resolved.is_relative_to(destination.resolve()):
                        raise BackupArchiveValidationError("備份路徑會離開匯入暫存目錄。")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info, "r") as source, target.open("wb") as output:
                        while chunk := source.read(1024 * 1024):
                            size += len(chunk)
                            digest.update(chunk)
                            output.write(chunk)
                    if size != record.size or digest.hexdigest() != record.sha256:
                        raise BackupArchiveValidationError(f"解壓縮時 checksum 不符：{name}")
                    if name.startswith("assets/"):
                        _validate_asset_file(target)
            _validate_snapshot_database(destination / DATABASE_ARCHIVE_PATH, set(record_map))
        except BackupArchiveError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupArchiveStorageError("無法安全解壓縮備份。") from exc

    def _install_extracted(self, extracted: Path) -> None:
        rollback = Path(
            tempfile.mkdtemp(prefix=".task-assignment-rollback-", dir=self.paths.base_dir.parent)
        )
        current_assets = self.paths.assets
        current_database = self.paths.database
        database_sidecars = (
            current_database.with_name(f"{current_database.name}-wal"),
            current_database.with_name(f"{current_database.name}-shm"),
        )
        installation_succeeded = False
        rollback_succeeded = False
        database_moved = False
        moved_sidecars: set[Path] = set()
        assets_moved = False
        database_install_started = False
        assets_install_started = False
        try:
            self.database.checkpoint()
            if current_database.exists():
                os.replace(current_database, rollback / current_database.name)
                database_moved = True
            for sidecar in database_sidecars:
                if sidecar.exists():
                    os.replace(sidecar, rollback / sidecar.name)
                    moved_sidecars.add(sidecar)
            if current_assets.exists():
                os.replace(current_assets, rollback / "assets")
                assets_moved = True
            database_install_started = True
            os.replace(extracted / DATABASE_ARCHIVE_PATH, current_database)
            assets_install_started = True
            self._move_assets(extracted / "assets", current_assets)
            _validate_snapshot_database(current_database, _current_asset_paths(current_assets))
            installation_succeeded = True
        except Exception as exc:
            rollback_errors: list[OSError] = []
            replaceable_paths: list[Path] = []
            if database_moved or database_install_started:
                replaceable_paths.append(current_database)
            replaceable_paths.extend(moved_sidecars)
            if assets_moved or assets_install_started:
                replaceable_paths.append(current_assets)
            for path in replaceable_paths:
                try:
                    _remove_path(path)
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
            restore_pairs = [(rollback / current_database.name, current_database)]
            restore_pairs.extend(
                (rollback / sidecar.name, sidecar) for sidecar in database_sidecars
            )
            restore_pairs.append((rollback / "assets", current_assets))
            for saved, destination in restore_pairs:
                if not saved.exists():
                    continue
                try:
                    os.replace(saved, destination)
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                raise BackupArchiveStorageError(
                    "匯入取代失敗，且自動恢復未完成；請保留程式資料並使用匯入前還原點。"
                ) from rollback_errors[0]
            rollback_succeeded = True
            if isinstance(exc, BackupArchiveError):
                raise
            raise BackupArchiveStorageError("匯入取代失敗，已恢復匯入前的資料。") from exc
        finally:
            if installation_succeeded or rollback_succeeded:
                shutil.rmtree(rollback, ignore_errors=True)

    def _move_assets(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def _unique_destination(self, prefix: str, created_at: datetime) -> Path:
        safe_prefix = re.sub(r"[^a-zA-Z0-9-]+", "-", prefix).strip("-")
        safe_prefix = safe_prefix or "task-assignment-backup"
        timestamp = created_at.strftime("%Y%m%d-%H%M%S")
        candidate = self.paths.backups / f"{safe_prefix}-{timestamp}.zip"
        counter = 2
        while candidate.exists():
            candidate = self.paths.backups / f"{safe_prefix}-{timestamp}-{counter}.zip"
            counter += 1
        return candidate


def _load_manifest(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise BackupArchiveValidationError("備份 manifest 必須是 JSON 物件。")
    return value


def _parse_manifest(path: Path, manifest: dict[str, Any]) -> ValidatedBackup:
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupArchiveValidationError("這不是 Task Assignment 備份。")
    if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
        raise BackupArchiveValidationError("備份格式版本與目前程式不相容。")
    if manifest.get("schema_version") != MIGRATIONS[-1].version:
        raise BackupArchiveValidationError("備份資料庫版本與目前程式不相容。")
    app_version = manifest.get("app_version")
    if not isinstance(app_version, str) or not app_version:
        raise BackupArchiveValidationError("備份 manifest 缺少應用程式版本。")
    try:
        created_at = datetime.fromisoformat(str(manifest["created_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupArchiveValidationError("備份建立時間格式不正確。") from exc
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise BackupArchiveValidationError("備份 manifest 缺少檔案清單。")
    records: list[ArchiveFileRecord] = []
    seen: set[str] = set()
    for raw_record in raw_files:
        if not isinstance(raw_record, dict):
            raise BackupArchiveValidationError("備份檔案清單格式不正確。")
        name = _safe_archive_path(str(raw_record.get("path", "")), allow_directory=False)
        if _path_key(name) in seen:
            raise BackupArchiveValidationError(f"manifest 含有重複路徑：{name}")
        seen.add(_path_key(name))
        if not _is_allowed_archive_path(name, False) or name == MANIFEST_ARCHIVE_PATH:
            raise BackupArchiveValidationError(f"manifest 含有不允許的檔案：{name}")
        size = raw_record.get("size")
        digest = raw_record.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise BackupArchiveValidationError(f"manifest 檔案大小不正確：{name}")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            raise BackupArchiveValidationError(f"manifest checksum 格式不正確：{name}")
        records.append(ArchiveFileRecord(name, size, digest))
    return ValidatedBackup(
        path=path,
        created_at=created_at,
        schema_version=MIGRATIONS[-1].version,
        app_version=app_version,
        files=tuple(records),
        archive_sha256=_sha256_file(path),
    )


def _safe_archive_path(value: str, *, allow_directory: bool) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise BackupArchiveValidationError("備份包含不安全或混合分隔符的路徑。")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[a-zA-Z]:", value):
        raise BackupArchiveValidationError("備份路徑不可使用絕對路徑或 ..。")
    normalized = path.as_posix()
    if allow_directory:
        normalized = f"{normalized.rstrip('/')}/"
    elif value.endswith("/"):
        raise BackupArchiveValidationError("manifest 檔案路徑不可是目錄。")
    return normalized


def _path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.casefold())


def _is_allowed_archive_path(name: str, is_directory: bool) -> bool:
    if name == MANIFEST_ARCHIVE_PATH or name == DATABASE_ARCHIVE_PATH:
        return not is_directory
    if is_directory:
        return name in {"assets/", BACKGROUND_DIRECTORY, STICKER_DIRECTORY}
    path = PurePosixPath(name)
    return (
        len(path.parts) == 3
        and path.parts[0] == "assets"
        and path.parts[1] in {"backgrounds", "stickers"}
        and bool(path.name)
        and not path.name.startswith(".")
    )


def _validate_snapshot_database(database_path: Path, payload_paths: set[str]) -> None:
    try:
        connection = sqlite3.connect(database_path)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackupArchiveValidationError("備份內的資料庫完整性檢查失敗。")
            foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_error is not None:
                raise BackupArchiveValidationError("備份資料庫含有無效的資料關聯。")
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not REQUIRED_DATABASE_TABLES <= table_names:
                raise BackupArchiveValidationError("備份資料庫缺少目前版本需要的資料表。")
            schema_row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            if schema_row is None or int(schema_row[0]) != MIGRATIONS[-1].version:
                raise BackupArchiveValidationError("備份資料庫 schema 版本不相容。")
            sensitive_keys = [
                str(row[0])
                for row in connection.execute("SELECT key FROM settings").fetchall()
                if _is_sensitive_setting_key(str(row[0]))
            ]
            if sensitive_keys:
                raise BackupArchiveValidationError("備份資料庫包含不允許的認證設定。")
            for kind, relative_path in connection.execute(
                "SELECT kind, relative_path FROM assets"
            ).fetchall():
                if kind not in {"background", "sticker"}:
                    raise BackupArchiveValidationError("備份內含有不支援的素材類型。")
                name = _safe_archive_path(str(relative_path), allow_directory=False)
                expected_root = f"assets/{'backgrounds' if kind == 'background' else 'stickers'}/"
                if not name.startswith(expected_root) or name not in payload_paths:
                    raise BackupArchiveValidationError("備份內的素材資料與實體檔案不一致。")
        finally:
            connection.close()
    except BackupArchiveError:
        raise
    except sqlite3.Error as exc:
        raise BackupArchiveValidationError("備份內的資料庫無法開啟。") from exc


def _remove_sensitive_settings(database_path: Path) -> None:
    try:
        connection = sqlite3.connect(database_path)
        try:
            keys = [row[0] for row in connection.execute("SELECT key FROM settings").fetchall()]
            for key in keys:
                if _is_sensitive_setting_key(str(key)):
                    connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupArchiveStorageError("無法排除備份中的敏感設定。") from exc


def _is_sensitive_setting_key(key: str) -> bool:
    lowered = key.casefold()
    return any(marker in lowered for marker in SENSITIVE_SETTING_MARKERS)


def _validate_asset_file(path: Path) -> None:
    if path.stat().st_size > MAX_ASSET_BYTES:
        raise BackupArchiveValidationError("備份內的單一素材不可超過 20 MB。")
    expected_format = SUPPORTED_ASSET_FORMATS.get(path.suffix.lower())
    if expected_format is None:
        raise BackupArchiveValidationError("備份包含不支援的素材格式。")
    reader = QImageReader(str(path))
    reader.setDecideFormatFromContent(True)
    actual_format = bytes(reader.format()).decode("ascii", errors="ignore").lower()
    if not reader.canRead() or actual_format != expected_format:
        raise BackupArchiveValidationError("備份素材的內容或副檔名不正確。")
    image = reader.read()
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        raise BackupArchiveValidationError("備份包含無法完整解碼的素材。")


def _hash_zip_entry(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "r") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _current_asset_paths(assets_root: Path) -> set[str]:
    paths = {DATABASE_ARCHIVE_PATH}
    if not assets_root.exists():
        return paths
    for item in assets_root.rglob("*"):
        if item.is_file() and not item.name.startswith("."):
            paths.add(item.relative_to(assets_root.parent).as_posix())
    return paths


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
