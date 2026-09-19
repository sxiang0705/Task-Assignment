from __future__ import annotations

import hashlib
import json
import runpy
import zipfile
from pathlib import Path

import pytest

BUILD_RELEASE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "tools" / "build_release.py")
)
scan_distribution = BUILD_RELEASE["_scan_distribution"]
build_release = BUILD_RELEASE["build_release"]
executable_name = BUILD_RELEASE["EXECUTABLE_NAME"]


@pytest.mark.parametrize("name", [
    "../outside.zip", r"..\outside.zip", "C:/outside.zip", "/outside.zip",
    "bad.zip:stream", "nested/file.zip", "CON.zip", "invalid.txt", "", ".zip",
])
def test_release_rejects_unsafe_output_before_writing(tmp_path, name):
    with pytest.raises(ValueError, match="ZIP 檔名"):
        build_release(tmp_path, name)
    assert not (tmp_path / "release").exists()


@pytest.mark.parametrize("existing_name", ["preview.zip", "preview.zip.sha256"])
def test_release_preserves_existing_output(tmp_path, existing_name):
    output = tmp_path / "release" / existing_name
    output.parent.mkdir()
    output.write_bytes(b"existing version")
    with pytest.raises(ValueError, match="已存在"):
        build_release(tmp_path, "preview.zip")
    assert output.read_bytes() == b"existing version"
    assert len(list(output.parent.iterdir())) == 1


def test_release_builds_new_archive_with_matching_checksum(tmp_path):
    _minimum_distribution(tmp_path / "dist")
    archive, checksum, scan = build_release(tmp_path, "preview.zip")
    assert scan.file_count == 2
    assert checksum.read_text().split()[0] == hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as result:
        assert result.testzip() is None
        assert "TaskAssignment/LOCAL-CANDIDATE.json" in result.namelist()
        manifest = json.loads(result.read("TaskAssignment/LOCAL-CANDIDATE.json"))
        assert manifest["performance_acceptance_seconds"] == {
            "startup": 10, "ordinary_query": 1, "dashboard": 2,
        }


def _minimum_distribution(root: Path) -> Path:
    distribution = root / "TaskAssignment"
    (distribution / "_internal" / "resources" / "icons").mkdir(parents=True)
    (distribution / executable_name).write_bytes(b"executable")
    (distribution / "_internal" / "resources" / "icons" / "task_assignment.svg").write_text(
        "<svg/>", encoding="utf-8"
    )
    return distribution


@pytest.mark.parametrize("invalid", [False, True])
def test_publisher_config_allows_desktop_but_rejects_tokens(tmp_path, invalid):
    distribution = _minimum_distribution(tmp_path)
    config = distribution / "_internal/resources/google_oauth_client.json"
    payload = {"installed": {
        "client_id": "test.apps.googleusercontent.com", "client_secret": "test-only",
    }}
    if invalid:
        payload["installed"]["refresh_token"] = "must-not-ship"
    config.write_text(json.dumps(payload), encoding="utf-8")
    if invalid:
        with pytest.raises(ValueError, match="設定無效"):
            scan_distribution(distribution, forbidden_paths=())
    else:
        assert scan_distribution(distribution, forbidden_paths=()).file_count == 3


def test_release_scan_accepts_minimum_clean_distribution(tmp_path: Path) -> None:
    distribution = _minimum_distribution(tmp_path)

    result = scan_distribution(distribution, forbidden_paths=(r"C:\private\project",))

    assert result.file_count == 2
    assert result.total_bytes > 0


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("data.sqlite3", b"database"),
        ("_internal/resources/google_oauth_client.json", b"{}"),
        ("_internal/tests/debug.txt", b"debug"),
        ("_internal/module.bin", rb"C:\private\project\src"),
    ),
)
def test_release_scan_rejects_private_or_development_content(
    tmp_path: Path,
    relative_path: str,
    content: bytes,
) -> None:
    distribution = _minimum_distribution(tmp_path)
    target = distribution / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)

    with pytest.raises(ValueError, match="release 掃描失敗"):
        scan_distribution(distribution, forbidden_paths=(r"C:\private\project",))
