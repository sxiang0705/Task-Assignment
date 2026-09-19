"""Validation and storage for user-supplied background and sticker images."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath
from uuid import uuid4

from PySide6.QtGui import QImageReader

from task_assignment.config import AppPaths

MAX_ASSET_BYTES = 20 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
EXPECTED_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
    ".gif": "gif",
}
ASSET_KINDS = {"background", "sticker"}


class AssetStoreError(RuntimeError):
    """Base error for safe asset storage operations."""


class AssetValidationError(AssetStoreError):
    """Raised when a selected image violates an import rule."""


class AssetStorageError(AssetStoreError):
    """Raised when a validated image cannot be copied or removed."""


class AssetStore:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def import_file(self, source_path: str | Path, kind: str) -> str:
        source = Path(source_path)
        target_directory = self._directory_for_kind(kind)
        self._validate_source(source)
        target_directory.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower()
        safe_stem = _safe_stem(source.stem)
        destination = target_directory / f"{safe_stem}-{uuid4().hex[:12]}{suffix}"
        temporary = target_directory / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, destination)
        except OSError as exc:
            _remove_if_present(temporary)
            _remove_if_present(destination)
            raise AssetStorageError("無法將圖片複製到應用程式資料目錄。") from exc
        return destination.relative_to(self.paths.base_dir).as_posix()

    def resolve(self, relative_path: str) -> Path:
        normalized = PurePosixPath(relative_path.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise AssetStorageError("素材路徑不是安全的應用程式相對路徑。")
        candidate = (self.paths.base_dir / Path(*normalized.parts)).resolve()
        asset_root = self.paths.assets.resolve()
        if not candidate.is_relative_to(asset_root):
            raise AssetStorageError("素材路徑超出應用程式素材目錄。")
        return candidate

    def delete(self, relative_path: str) -> None:
        path = self.resolve(relative_path)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise AssetStorageError("無法刪除素材檔案，請確認檔案未被其他程式使用。") from exc

    def stage_delete(self, relative_path: str) -> Path | None:
        path = self.resolve(relative_path)
        if not path.exists():
            return None
        staged = path.with_name(f".{path.name}.{uuid4().hex}.deleting")
        try:
            os.replace(path, staged)
        except OSError as exc:
            raise AssetStorageError("無法準備刪除素材，請確認檔案未被其他程式使用。") from exc
        return staged

    def restore_staged(self, relative_path: str, staged: Path | None) -> None:
        if staged is None or not staged.exists():
            return
        destination = self.resolve(relative_path)
        try:
            os.replace(staged, destination)
        except OSError as exc:
            raise AssetStorageError("素材刪除失敗，且無法恢復原始檔案。") from exc

    def finalize_staged(self, staged: Path | None) -> None:
        if staged is None:
            return
        try:
            staged.unlink(missing_ok=True)
        except OSError as exc:
            raise AssetStorageError("素材已停用，但暫存檔案無法清除。") from exc

    def _directory_for_kind(self, kind: str) -> Path:
        if kind == "background":
            return self.paths.backgrounds
        if kind == "sticker":
            return self.paths.stickers
        raise AssetValidationError("素材類型只支援背景或貼圖。")

    def _validate_source(self, source: Path) -> None:
        if not source.is_file():
            raise AssetValidationError("找不到選取的圖片檔案。")
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise AssetValidationError("只支援 PNG、JPG、JPEG、WebP 與 GIF 圖片。")
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise AssetStorageError("無法讀取選取的圖片檔案。") from exc
        if size <= 0:
            raise AssetValidationError("圖片檔案不可為空白。")
        if size > MAX_ASSET_BYTES:
            raise AssetValidationError("單一圖片不可超過 20 MB。")

        reader = QImageReader(str(source))
        reader.setDecideFormatFromContent(True)
        actual_format = bytes(reader.format()).decode("ascii", errors="ignore").lower()
        if not reader.canRead():
            raise AssetValidationError("選取的檔案不是可解碼的圖片。")
        if actual_format != EXPECTED_FORMATS[suffix]:
            raise AssetValidationError("圖片內容與副檔名不一致，請選擇正確的圖片檔案。")
        image = reader.read()
        if image.isNull() or image.width() <= 0 or image.height() <= 0:
            raise AssetValidationError("選取的圖片無法完整解碼。")


def _safe_stem(value: str) -> str:
    normalized = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return (normalized or "image")[:48]


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
