from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from task_assignment.application import (
    ServiceConflictError,
    ServiceNotFoundError,
    ServiceValidationError,
    TaskDraft,
    TaskQuery,
    TaskSort,
    create_services,
)
from task_assignment.application.services import ApplicationServices
from task_assignment.config import AppPaths
from task_assignment.domain.enums import ScheduleMode, ScheduleStatus, TaskStatus
from task_assignment.infrastructure.database.connection import Database
from task_assignment.infrastructure.database.repositories import RepositoryError

NOW = datetime(2026, 1, 10, 12, 0)


@pytest.fixture
def service_context(tmp_path: Path) -> tuple[Database, ApplicationServices]:
    database = Database(tmp_path / "task_assignment.db")
    database.initialize()
    return database, create_services(database, now_provider=lambda: NOW)


def manual_draft(
    name: str,
    *days: int,
    start_at: datetime = datetime(2026, 1, 8, 9, 30),
    **changes: object,
) -> TaskDraft:
    values: dict[str, object] = {
        "name": name,
        "start_at": start_at,
        "schedule_mode": ScheduleMode.MANUAL,
        "manual_schedule_times": tuple(datetime(2026, 1, day, 9, 30) for day in days),
    }
    values.update(changes)
    return TaskDraft(**values)  # type: ignore[arg-type]


def write_test_image(path: Path, image_format: str) -> None:
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(0xFF5A8F7D)
    assert image.save(str(path), image_format)


def test_val_schedule_007_task_001_preview_matches_created_dto(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-SCHEDULE-007/VAL-TASK-001: preview equals persisted DTO output."""

    _, services = service_context
    category = services.tasks.create_category("證照")
    tag = services.tasks.create_tag("Python")
    draft = TaskDraft(
        name="  資料工程  ",
        description="準備考試",
        category_id=category.id,
        tag_ids=(tag.id,),
        start_at=datetime(2026, 1, 10, 9, 30),
        schedule_mode=ScheduleMode.CURVE,
        review_count=3,
    )

    preview = services.tasks.preview_schedule(draft)
    created = services.tasks.create(draft)

    assert tuple(item.scheduled_at for item in created.schedules) == preview.scheduled_at
    assert created.task.name == "資料工程"
    assert created.task.category == category
    assert created.task.tags == (tag,)
    with pytest.raises(ServiceValidationError) as raised:
        services.tasks.create(replace(draft, name="   "))
    assert raised.value.code == "validation_error"


def test_val_schedule_008_service_edit_preserves_history_and_is_atomic(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-SCHEDULE-008: edit changes fields/tags/pending rows as one transaction."""

    _, services = service_context
    original_tag = services.tasks.create_tag("原標籤")
    replacement_tag = services.tasks.create_tag("新標籤")
    created = services.tasks.create(
        TaskDraft(
            name="原任務",
            tag_ids=(original_tag.id,),
            start_at=datetime(2026, 1, 1, 9, 30),
            schedule_mode=ScheduleMode.CURVE,
            review_count=3,
        )
    )
    services.reviews.complete(created.schedules[0].id)
    before = services.tasks.details(created.task.id)
    changed = TaskDraft(
        name="修改後",
        tag_ids=(replacement_tag.id,),
        start_at=datetime(2026, 1, 2, 9, 30),
        schedule_mode=ScheduleMode.CURVE,
        review_count=3,
    )
    preview = services.tasks.preview_change(created.task.id, changed)

    with pytest.raises(ServiceConflictError):
        services.tasks.update(created.task.id, replace(changed, tag_ids=(999_999,)))
    assert services.tasks.details(created.task.id) == before

    updated = services.tasks.update(created.task.id, changed)
    assert updated.task.name == "修改後"
    assert updated.task.tags == (replacement_tag,)
    assert updated.schedules[0].id == before.schedules[0].id
    assert updated.schedules[0].status is ScheduleStatus.COMPLETED
    assert preview.preserved_history == (before.schedules[0],)
    assert preview.removed_pending == (
        datetime(2026, 1, 4, 9, 30),
        datetime(2026, 1, 8, 9, 30),
    )
    assert preview.added_pending == (
        datetime(2026, 1, 5, 9, 30),
        datetime(2026, 1, 9, 9, 30),
    )


def test_val_schedule_009_rejected_edit_leaves_everything_unchanged(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-SCHEDULE-009: fewer proposed rows than history cannot partially save."""

    _, services = service_context
    created = services.tasks.create(
        TaskDraft(
            name="四次複習",
            start_at=datetime(2026, 1, 1, 9, 30),
            schedule_mode=ScheduleMode.CURVE,
            review_count=4,
        )
    )
    for schedule in created.schedules:
        services.reviews.complete(schedule.id)
    before = services.tasks.details(created.task.id)

    with pytest.raises(ServiceValidationError):
        services.tasks.update(
            created.task.id,
            replace(
                TaskDraft(
                    name="不應儲存",
                    start_at=datetime(2026, 1, 1, 9, 30),
                    schedule_mode=ScheduleMode.CURVE,
                    review_count=3,
                )
            ),
        )

    assert services.tasks.details(created.task.id) == before


def test_val_task_005_duplicate_manual_task_gets_new_id_and_shifted_schedule(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-TASK-005: duplication preserves content without touching the source."""

    _, services = service_context
    tag = services.tasks.create_tag("複製")
    original = services.tasks.create(manual_draft("原稿", 11, 13, tag_ids=(tag.id,)))
    duplicate = services.tasks.duplicate(
        original.task.id,
        start_at=datetime(2026, 1, 18, 9, 30),
    )

    assert duplicate.task.id != original.task.id
    assert duplicate.task.name == "原稿（副本）"
    assert duplicate.task.tags == (tag,)
    assert [item.scheduled_at.day for item in duplicate.schedules] == [21, 23]
    assert services.tasks.details(original.task.id) == original


def test_val_search_001_to_004_and_task_010_combined_query_and_sort(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-SEARCH-001..004/VAL-TASK-010: DTO query composes all filters."""

    _, services = service_context
    learning = services.tasks.create_category("學習")
    work = services.tasks.create_category("工作")
    python_tag = services.tasks.create_tag("Python")
    urgent_tag = services.tasks.create_tag("急件")
    alpha = services.tasks.create(
        manual_draft(
            "Alpha",
            12,
            description="資料結構",
            category_id=learning.id,
            tag_ids=(python_tag.id,),
        )
    )
    beta = services.tasks.create(
        manual_draft(
            "Beta",
            9,
            category_id=work.id,
            tag_ids=(urgent_tag.id,),
        )
    )
    archived = services.tasks.create(manual_draft("Gamma", 15))
    services.tasks.archive(archived.task.id)

    assert [item.id for item in services.tasks.query(TaskQuery(text="學習"))] == [alpha.task.id]
    assert [item.id for item in services.tasks.query(TaskQuery(tag_id=urgent_tag.id))] == [
        beta.task.id
    ]
    assert [
        item.id
        for item in services.tasks.query(TaskQuery(statuses=frozenset({TaskStatus.OVERDUE})))
    ] == [beta.task.id]
    assert [
        item.id
        for item in services.tasks.query(
            TaskQuery(scheduled_from=date(2026, 1, 12), scheduled_to=date(2026, 1, 12))
        )
    ] == [alpha.task.id]
    assert [item.name for item in services.tasks.query(TaskQuery(sort_by=TaskSort.NAME))] == [
        "Alpha",
        "Beta",
    ]
    assert len(services.tasks.query(TaskQuery(include_archived=True))) == 3


def test_val_task_007_refreshes_stored_status_after_local_date_change(tmp_path: Path) -> None:
    """VAL-TASK-007: explicit startup/date refresh writes new task status."""

    database = Database(tmp_path / "task_assignment.db")
    database.initialize()
    current = [datetime(2026, 1, 10, 12)]
    services = create_services(database, now_provider=lambda: current[0])
    created = services.tasks.create(manual_draft("跨日", 11))
    assert created.task.status is TaskStatus.IN_PROGRESS

    current[0] = datetime(2026, 1, 11, 0, 1)
    services.tasks.refresh_all_statuses()

    assert services.tasks.details(created.task.id).task.status is TaskStatus.DUE_TODAY


def test_task_service_pause_resume_archive_restore_and_delete(tmp_path: Path) -> None:
    database = Database(tmp_path / "task_assignment.db")
    database.initialize()
    current = [datetime(2026, 1, 10, 12)]
    services = create_services(database, now_provider=lambda: current[0])
    created = services.tasks.create(manual_draft("生命週期", 11))

    paused = services.tasks.pause(created.task.id)
    assert paused.task.status is TaskStatus.PAUSED
    with pytest.raises(ServiceConflictError):
        services.tasks.pause(created.task.id)

    current[0] = datetime(2026, 1, 12, 12)
    resumed = services.tasks.resume(created.task.id)
    assert resumed.task.is_paused is False
    assert resumed.schedules[0].scheduled_at == datetime(2026, 1, 13, 9, 30)

    services.tasks.archive(created.task.id)
    assert services.tasks.query() == ()
    services.tasks.restore(created.task.id)
    assert [task.id for task in services.tasks.query()] == [created.task.id]

    services.tasks.delete(created.task.id)
    with pytest.raises(ServiceNotFoundError):
        services.tasks.details(created.task.id)


def test_val_review_001_date_002_006_lists_rich_local_date_items(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-REVIEW-001/VAL-DATE-002/006: review DTO and seven-day boundary."""

    _, services = service_context
    category = services.tasks.create_category("語言")
    tag = services.tasks.create_tag("英文")
    created = services.tasks.create(
        TaskDraft(
            name="單字",
            description="每日清單",
            category_id=category.id,
            tag_ids=(tag.id,),
            start_at=datetime(2026, 1, 8, 9),
            schedule_mode=ScheduleMode.MANUAL,
            manual_schedule_times=(
                datetime(2026, 1, 9, 23, 59),
                datetime(2026, 1, 10, 1),
                datetime(2026, 1, 10, 23),
                datetime(2026, 1, 16, 9),
                datetime(2026, 1, 17, 9),
            ),
        )
    )

    today = services.reviews.due_today()
    assert len(today) == 2
    assert today[0].task_name == "單字"
    assert today[0].description == "每日清單"
    assert today[0].category == category
    assert today[0].tags == (tag,)
    assert [item.schedule_id for item in services.reviews.overdue()] == [created.schedules[0].id]
    assert [item.scheduled_at.day for item in services.reviews.next_seven_days()] == [
        10,
        10,
        16,
    ]


def test_val_review_002_007_010_actions_return_updated_result_and_safe_errors(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-REVIEW-002/007/010: actions expose refreshed DTOs and validation."""

    _, services = service_context
    created = services.tasks.create(manual_draft("操作", 10, 11, 12))
    completed = services.reviews.complete(created.schedules[0].id)
    assert completed.completion_rate == pytest.approx(100 / 3)
    assert completed.schedules[0].status is ScheduleStatus.COMPLETED

    postponed = services.reviews.postpone(created.schedules[1].id)
    assert [item.scheduled_at.day for item in postponed.schedules] == [10, 12, 13]

    rescheduled = services.reviews.reschedule(created.schedules[1].id, datetime(2026, 1, 14, 8))
    assert rescheduled.schedules[1].scheduled_at == datetime(2026, 1, 14, 8)
    with pytest.raises(ServiceValidationError):
        services.reviews.reschedule(created.schedules[1].id, datetime(2026, 1, 7, 8))
    with pytest.raises(ServiceConflictError):
        services.reviews.reschedule(created.schedules[1].id, datetime(2026, 1, 13, 9, 30))
    assert services.tasks.details(created.task.id).schedules[1].scheduled_at == datetime(
        2026, 1, 14, 8
    )


def test_val_search_005_006_dashboard_excludes_paused_and_archived(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-SEARCH-005/006: summary applies active-only and skipped denominator rules."""

    _, services = service_context
    active = services.tasks.create(manual_draft("統計", 9, 10, 12))
    services.reviews.complete(active.schedules[0].id)
    services.reviews.skip(active.schedules[1].id)
    paused = services.tasks.create(manual_draft("暫停", 10))
    services.tasks.pause(paused.task.id)
    archived = services.tasks.create(manual_draft("封存", 10))
    services.tasks.archive(archived.task.id)

    summary = services.tasks.dashboard_summary()

    assert summary.task_total == 1
    assert summary.pending_review_count == 1
    assert summary.overdue_count == 0
    assert summary.due_today_count == 0
    assert summary.next_seven_days_count == 1
    assert summary.completed_review_count == 1
    assert summary.incomplete_review_count == 2
    assert summary.completion_rate == pytest.approx(100 / 3)


def test_val_calendar_002_003_006_counts_refresh_after_task_changes(
    service_context: tuple[Database, ApplicationServices],
) -> None:
    """VAL-CALENDAR-002/003/006: calendar counts reflect actions and active tasks."""

    _, services = service_context
    active = services.tasks.create(manual_draft("月曆", 9, 10, 11))
    services.reviews.complete(active.schedules[1].id)
    services.reviews.skip(active.schedules[2].id)
    paused = services.tasks.create(manual_draft("不顯示", 9))
    services.tasks.pause(paused.task.id)

    calendar = services.tasks.calendar(date(2026, 1, 9), date(2026, 1, 11))
    assert calendar[0].schedule_count == 1
    assert calendar[0].overdue_count == 1
    assert calendar[1].completed_count == 1
    assert calendar[2].skipped_count == 1

    services.tasks.archive(active.task.id)
    assert all(
        day.schedule_count == 0
        for day in services.tasks.calendar(date(2026, 1, 9), date(2026, 1, 11))
    )


def test_settings_and_not_found_errors_never_expose_repository_types(
    service_context: tuple[Database, ApplicationServices], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, services = service_context
    assert services.settings.get("theme", "system") == "system"
    services.settings.set("theme", {"mode": "dark"}, now=NOW)
    assert services.settings.get("theme") == {"mode": "dark"}
    with pytest.raises(ServiceValidationError):
        services.settings.set("theme", object())
    with pytest.raises(ServiceNotFoundError) as raised:
        services.tasks.details(999_999)
    assert raised.value.code == "not_found"

    def fail_read() -> tuple[()]:
        raise sqlite3.OperationalError("sensitive internal database detail")

    monkeypatch.setattr(services.tasks.tasks, "list_all", fail_read)
    with pytest.raises(ServiceConflictError) as storage_error:
        services.tasks.query()
    assert "sensitive" not in storage_error.value.user_message


@pytest.mark.parametrize(
    ("suffix", "image_format"),
    ((".png", "PNG"), (".jpg", "JPG"), (".jpeg", "JPEG"), (".webp", "WEBP")),
)
def test_val_asset_001_003_008_supported_images_are_copied_to_owned_storage(
    tmp_path: Path, suffix: str, image_format: str
) -> None:
    """VAL-ASSET-001/003/008: decoded images survive deletion of their source."""

    paths = AppPaths.from_base_dir(tmp_path / "app-data")
    paths.ensure_directories()
    database = Database(paths.database)
    database.initialize()
    services = create_services(database, paths=paths)
    source = tmp_path / f"學習背景{suffix}"
    write_test_image(source, image_format)

    asset = services.assets.import_asset(source, "background")
    source.unlink()

    assert asset.relative_path.startswith("assets/backgrounds/")
    assert not Path(asset.relative_path).is_absolute()
    assert asset.absolute_path.is_file()
    assert services.assets.list_assets("background") == (asset,)


def test_val_asset_002_rejects_oversize_invalid_and_disguised_files(
    service_context: tuple[Database, ApplicationServices], tmp_path: Path
) -> None:
    """VAL-ASSET-002: size, decoding, and extension/content checks are enforced."""

    _, services = service_context
    oversized = tmp_path / "too-large.png"
    with oversized.open("wb") as stream:
        stream.seek((20 * 1024 * 1024) + 1)
        stream.write(b"0")
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not an image")
    disguised = tmp_path / "disguised.jpg"
    write_test_image(disguised, "PNG")

    with pytest.raises(ServiceValidationError, match="20 MB"):
        services.assets.import_asset(oversized, "background")
    with pytest.raises(ServiceValidationError, match="可解碼"):
        services.assets.import_asset(invalid, "background")
    with pytest.raises(ServiceValidationError, match="副檔名"):
        services.assets.import_asset(disguised, "background")


def test_val_asset_004_005_background_scope_and_fixed_sticker_selection(
    service_context: tuple[Database, ApplicationServices], tmp_path: Path
) -> None:
    """VAL-ASSET-004/005: background scope and sticker enabled state round-trip."""

    _, services = service_context
    global_source = tmp_path / "global.png"
    page_source = tmp_path / "page.png"
    sticker_source = tmp_path / "sticker.webp"
    write_test_image(global_source, "PNG")
    write_test_image(page_source, "PNG")
    write_test_image(sticker_source, "WEBP")
    global_background = services.assets.import_asset(global_source, "background")
    page_background = services.assets.import_asset(page_source, "background")
    sticker = services.assets.import_asset(sticker_source, "sticker")

    services.assets.configure_background(global_background.id, mode="global")
    assert services.assets.background_for("reviews") == global_background
    services.assets.configure_background(page_background.id, mode="page", page_id="calendar")
    assert services.assets.background_for("calendar") == page_background
    assert services.assets.background_for("reviews") is None
    services.assets.configure_sticker(sticker.id, enabled=True)
    assert services.assets.sticker() == sticker
    reloaded = create_services(
        services.assets.assets.database,
        paths=services.assets.store.paths,
        now_provider=lambda: NOW,
    )
    assert reloaded.assets.background_for("calendar") == page_background
    assert reloaded.assets.sticker() == sticker

    services.assets.configure_sticker(sticker.id, enabled=False)
    assert services.assets.sticker() is None
    with pytest.raises(ServiceValidationError, match="類型"):
        services.assets.configure_background(sticker.id, mode="global")


def test_val_asset_006_delete_active_assets_restores_defaults(
    service_context: tuple[Database, ApplicationServices], tmp_path: Path
) -> None:
    """VAL-ASSET-006: deleting selected assets clears background and sticker state."""

    _, services = service_context
    background_source = tmp_path / "background.png"
    sticker_source = tmp_path / "sticker.png"
    write_test_image(background_source, "PNG")
    write_test_image(sticker_source, "PNG")
    background = services.assets.import_asset(background_source, "background")
    sticker = services.assets.import_asset(sticker_source, "sticker")
    services.assets.configure_background(background.id, mode="global")
    services.assets.configure_sticker(sticker.id, enabled=True)

    services.assets.delete_asset(background.id)
    services.assets.delete_asset(sticker.id)

    appearance = services.assets.appearance()
    assert appearance.global_background_id is None
    assert appearance.sticker_asset_id is None
    assert not appearance.sticker_enabled
    assert not background.absolute_path.exists()
    assert not sticker.absolute_path.exists()
    assert services.assets.list_assets() == ()


def test_val_asset_007_failed_metadata_write_cleans_copy_and_failed_delete_restores_file(
    service_context: tuple[Database, ApplicationServices],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VAL-ASSET-007: file and database stay synchronized after injected failures."""

    _, services = service_context
    source = tmp_path / "atomic.png"
    write_test_image(source, "PNG")

    def fail_add(*_args: object, **_kwargs: object) -> None:
        raise RepositoryError("injected add failure")

    monkeypatch.setattr(services.assets.assets, "add", fail_add)
    with pytest.raises(ServiceConflictError):
        services.assets.import_asset(source, "background")
    assert tuple(services.assets.store.paths.backgrounds.iterdir()) == ()

    monkeypatch.undo()
    asset = services.assets.import_asset(source, "background")

    def fail_delete(*_args: object, **_kwargs: object) -> None:
        raise RepositoryError("injected delete failure")

    monkeypatch.setattr(services.assets.assets, "delete_with_setting", fail_delete)
    with pytest.raises(ServiceConflictError):
        services.assets.delete_asset(asset.id)
    assert asset.absolute_path.is_file()
    assert services.assets.list_assets("background") == (asset,)
