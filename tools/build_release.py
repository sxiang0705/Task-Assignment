"""Validate and archive the local PyInstaller release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from task_assignment.infrastructure.gmail.oauth import OAuthClientConfig
from task_assignment.version import APP_VERSION, RELEASE_UPDATED_AT

FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
FORBIDDEN_FILENAMES = {
    "credentials.json",
    "google_oauth_client.json",
    "token.json",
}
FORBIDDEN_PARTS = {"artifacts", "backups", "logs", "tests"}
EXECUTABLE_NAME = "遺忘曲線大禮包 必上岸版本.exe"


@dataclass(frozen=True)
class ScanResult:
    file_count: int
    total_bytes: int


def _scan_distribution(distribution: Path, *, forbidden_paths: tuple[str, ...]) -> ScanResult:
    if not distribution.is_dir():
        raise ValueError(f"找不到封裝目錄：{distribution}")
    if not (distribution / EXECUTABLE_NAME).is_file():
        raise ValueError(f"封裝目錄缺少 {EXECUTABLE_NAME}")
    if not (distribution / "_internal" / "resources" / "icons" / "task_assignment.svg").is_file():
        raise ValueError("封裝目錄缺少應用程式圖示")

    files = sorted(path for path in distribution.rglob("*") if path.is_file())
    violations: list[str] = []
    total_bytes = 0
    encoded_paths = tuple(
        encoded
        for value in forbidden_paths
        for encoded in (
            value.encode("utf-8", errors="ignore"),
            value.encode("utf-16-le", errors="ignore"),
        )
        if encoded
    )

    for path in files:
        relative = path.relative_to(distribution)
        lowered_parts = {part.lower() for part in relative.parts}
        lowered_name = path.name.lower()
        total_bytes += path.stat().st_size
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            violations.append(f"含資料庫：{relative}")
        publisher_config = relative.as_posix() == "_internal/resources/google_oauth_client.json"
        if publisher_config:
            try:
                OAuthClientConfig.from_payload(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, ValueError):
                violations.append(f"發布者 Desktop OAuth 設定無效：{relative}")
        elif lowered_name in FORBIDDEN_FILENAMES:
            violations.append(f"含認證檔：{relative}")
        if lowered_parts & FORBIDDEN_PARTS:
            violations.append(f"含開發或使用者資料目錄：{relative}")
        payload = path.read_bytes()
        if any(marker in payload for marker in encoded_paths):
            violations.append(f"含本機絕對路徑：{relative}")

    if violations:
        raise ValueError("release 掃描失敗：\n- " + "\n- ".join(violations))
    return ScanResult(file_count=len(files), total_bytes=total_bytes)


def build_release(project_root: Path, output_name: str) -> tuple[Path, Path, ScanResult]:
    if (
        not output_name
        or any(char in output_name for char in '/\\:<>"|?*')
        or any(ord(char) < 32 for char in output_name)
        or output_name != output_name.strip()
        or not output_name.lower().endswith(".zip")
        or output_name.startswith(".")
        or output_name.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
    ):
        raise ValueError("輸出必須是單一有效 ZIP 檔名，不可包含路徑。")
    project_root = project_root.resolve()
    distribution = project_root / "dist" / "TaskAssignment"
    release_dir = project_root / "release"
    if release_dir.resolve().parent != project_root:
        raise ValueError("release 目錄不可指向專案外部。")
    archive_path = release_dir / output_name
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if any(path.exists() or path.is_symlink() for path in (archive_path, checksum_path)):
        raise ValueError("輸出 ZIP 或校驗檔已存在，請使用新的檔名；不會覆寫既有版本。")
    scan = _scan_distribution(
        distribution,
        forbidden_paths=(str(project_root), str(Path.home())),
    )
    release_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "app": "Task Assignment",
        "version": APP_VERSION,
        "release_updated_at": RELEASE_UPDATED_AT,
        "candidate_kind": "personal-use-local-validation",
        "file_count": scan.file_count,
        "total_bytes": scan.total_bytes,
        "gmail_oauth_credentials_included": False,
        "gmail_oauth_client_config_included": True,
        "performance_acceptance_seconds": {
            "startup": 10,
            "ordinary_query": 1,
            "dashboard": 2,
        },
        "release_blockers": [
            "Clean Windows 10/11 validation without Python (optional for personal deployment)",
            "Cold-start and 150%/200% DPI validation on the reference machine (optional)",
        ],
    }
    with zipfile.ZipFile(
        archive_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for source in sorted(path for path in distribution.rglob("*") if path.is_file()):
            relative = Path("TaskAssignment") / source.relative_to(distribution)
            archive.write(source, relative.as_posix())
        archive.writestr(
            "TaskAssignment/LOCAL-CANDIDATE.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with checksum_path.open("x", encoding="utf-8") as checksum:
        checksum.write(f"{digest}  {archive_path.name}\n")
    return archive_path, checksum_path, scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=f"TaskAssignment-v{APP_VERSION}-testing-win64.zip",
        help="ZIP file name written under release/",
    )
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    try:
        archive, checksum, scan = build_release(project_root, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        f"release candidate: {archive}\n"
        f"checksum: {checksum}\n"
        f"scanned: {scan.file_count} files, {scan.total_bytes} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
