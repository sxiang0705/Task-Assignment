"""Application services composing domain rules and SQLite repositories."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from email.headerregistry import Address
from pathlib import Path

from task_assignment.application.dto import (
    AppearanceSettings,
    AssetView,
    BackupResult,
    CalendarDaySummary,
    CatalogItem,
    DashboardSummary,
    GmailAccountStatus,
    GmailDisconnectResult,
    GmailDraft,
    GmailSendResult,
    ImportResult,
    ReviewActionResult,
    ReviewItem,
    ReviewPage,
    ScheduleChangePreview,
    SchedulePreview,
    ScheduleView,
    TaskDetails,
    TaskDraft,
    TaskPage,
    TaskQuery,
    TaskSort,
    TaskView,
)
from task_assignment.application.errors import (
    ServiceConflictError,
    ServiceError,
    ServiceNotFoundError,
    ServiceValidationError,
)
from task_assignment.config import AppPaths
from task_assignment.domain.enums import ScheduleMode, ScheduleStatus, TaskStatus
from task_assignment.domain.errors import DomainError
from task_assignment.domain.models import ReviewSchedule, Task
from task_assignment.domain.review import rebuild_pending_schedules
from task_assignment.domain.scheduler import generate_curve_schedule, validate_manual_schedule
from task_assignment.infrastructure.backup import (
    BackupArchive,
    BackupArchiveError,
    BackupArchiveStorageError,
    BackupArchiveValidationError,
)
from task_assignment.infrastructure.database.connection import Database, DatabaseError
from task_assignment.infrastructure.database.repositories import (
    AssetEntry,
    AssetRepository,
    BackupLogEntry,
    BackupLogRepository,
    CatalogRepository,
    EntityNotFoundError,
    RepositoryError,
    ReviewRepository,
    SettingsRepository,
    TaskRepository,
)
from task_assignment.infrastructure.files import (
    AssetStorageError,
    AssetStore,
    AssetStoreError,
    AssetValidationError,
)
from task_assignment.infrastructure.gmail import (
    CredentialRecord,
    CredentialStore,
    CredentialStoreError,
    GmailApiClient,
    GmailAuthorizationError,
    GmailConfigurationError,
    GmailGateway,
    GmailInfrastructureError,
    GmailMessageError,
    GmailNetworkError,
    GmailReauthorizationRequired,
    GoogleOAuthClient,
    OAuthClientConfig,
    OAuthGateway,
    OAuthTokens,
    WindowsCredentialStore,
)

NowProvider = Callable[[], datetime]


class TaskService:
    def __init__(
        self,
        tasks: TaskRepository,
        reviews: ReviewRepository,
        catalog: CatalogRepository,
        *,
        now_provider: NowProvider = datetime.now,
    ) -> None:
        self.tasks = tasks
        self.reviews = reviews
        self.catalog = catalog
        self.now_provider = now_provider

    def preview_schedule(self, draft: TaskDraft) -> SchedulePreview:
        with _service_errors():
            return SchedulePreview(self._schedule_times(draft))

    def create(self, draft: TaskDraft) -> TaskDetails:
        with _service_errors():
            now = self.now_provider()
            schedule_times = self._schedule_times(draft)
            task = self.tasks.create(
                Task(
                    name=draft.name.strip(),
                    description=draft.description,
                    category_id=draft.category_id,
                    start_at=draft.start_at,
                    schedule_mode=draft.schedule_mode,
                    review_count=draft.review_count,
                ),
                schedule_times,
                tag_ids=draft.tag_ids,
                now=now,
            )
            return self._details_for(task)

    def details(self, task_id: int) -> TaskDetails:
        with _service_errors():
            return self._details_for(self.tasks.get(task_id))

    def preview_change(self, task_id: int, draft: TaskDraft) -> ScheduleChangePreview:
        with _service_errors():
            existing_task = self.tasks.get(task_id)
            existing = self.reviews.list_for_task(task_id)
            proposed = self._schedule_times(draft)
            rebuilt = rebuild_pending_schedules(
                existing,
                proposed,
                task_id=_persisted_id(existing_task),
                start_at=draft.start_at,
            )
            old_pending = {
                item.scheduled_at for item in existing if item.status is ScheduleStatus.PENDING
            }
            new_pending = {
                item.scheduled_at for item in rebuilt if item.status is ScheduleStatus.PENDING
            }
            preserved = tuple(
                _schedule_view(item)
                for item in rebuilt
                if item.status is not ScheduleStatus.PENDING
            )
            return ScheduleChangePreview(
                preserved_history=preserved,
                unchanged_pending=tuple(sorted(old_pending & new_pending)),
                removed_pending=tuple(sorted(old_pending - new_pending)),
                added_pending=tuple(sorted(new_pending - old_pending)),
            )

    def update(self, task_id: int, draft: TaskDraft) -> TaskDetails:
        with _service_errors():
            existing = self.tasks.get(task_id)
            now = self.now_provider()
            schedule_times = self._schedule_times(draft)
            updated = self.tasks.update_with_schedules(
                replace(
                    existing,
                    name=draft.name.strip(),
                    description=draft.description,
                    category_id=draft.category_id,
                    start_at=draft.start_at,
                    schedule_mode=draft.schedule_mode,
                    review_count=draft.review_count,
                ),
                schedule_times,
                tag_ids=draft.tag_ids,
                today=now.date(),
                now=now,
            )
            return self._details_for(updated)

    def duplicate(
        self,
        task_id: int,
        *,
        name: str | None = None,
        start_at: datetime | None = None,
    ) -> TaskDetails:
        with _service_errors():
            original = self._details_for(self.tasks.get(task_id))
            target_start = start_at or original.task.start_at
            if original.task.schedule_mode is ScheduleMode.CURVE:
                manual_times: tuple[datetime, ...] = ()
            else:
                shift = target_start - original.task.start_at
                manual_times = tuple(
                    schedule.scheduled_at + shift for schedule in original.schedules
                )
            draft = TaskDraft(
                name=name if name is not None else f"{original.task.name}（副本）",
                description=original.task.description,
                category_id=original.task.category.id if original.task.category else None,
                tag_ids=tuple(tag.id for tag in original.task.tags),
                start_at=target_start,
                schedule_mode=original.task.schedule_mode,
                review_count=original.task.review_count,
                manual_schedule_times=manual_times,
            )
            return self.create(draft)

    def delete(self, task_id: int) -> None:
        with _service_errors():
            self.tasks.delete(task_id)

    def pause(self, task_id: int) -> TaskDetails:
        with _service_errors():
            now = self.now_provider()
            if self.tasks.get(task_id).is_paused:
                raise ServiceConflictError("任務已經處於暫停狀態。")
            return self._details_for(self.tasks.set_paused(task_id, paused_on=now.date(), now=now))

    def resume(self, task_id: int) -> TaskDetails:
        with _service_errors():
            now = self.now_provider()
            return self._details_for(self.tasks.resume(task_id, resumed_on=now.date(), now=now))

    def archive(self, task_id: int) -> TaskDetails:
        return self._set_archived(task_id, archived=True)

    def restore(self, task_id: int) -> TaskDetails:
        return self._set_archived(task_id, archived=False)

    def _set_archived(self, task_id: int, *, archived: bool) -> TaskDetails:
        with _service_errors():
            return self._details_for(
                self.tasks.set_archived(task_id, archived=archived, now=self.now_provider())
            )

    def refresh_all_statuses(self) -> None:
        with _service_errors():
            now = self.now_provider()
            self.tasks.refresh_all_metrics(today=now.date(), now=now)

    def query(self, query: TaskQuery | None = None) -> tuple[TaskView, ...]:
        with _service_errors():
            query = query or TaskQuery()
            if (
                query.scheduled_from is not None
                and query.scheduled_to is not None
                and query.scheduled_to < query.scheduled_from
            ):
                raise ServiceValidationError("日期區間的結束日期不可早於開始日期。")
            candidates = (
                self.tasks.search(query.text) if query.text.strip() else self.tasks.list_all()
            )
            schedules_by_task = _group_schedules(self.reviews.list_for_tasks(_task_ids(candidates)))
            tag_ids_by_task = self.tasks.tag_ids_by_task(_task_ids(candidates))
            categories, tags = _catalog_maps(self.catalog)

            views: list[TaskView] = []
            for task in candidates:
                task_id = _persisted_id(task)
                schedules = schedules_by_task.get(task_id, ())
                if (
                    task.is_archived
                    and not query.include_archived
                    and TaskStatus.ARCHIVED not in query.statuses
                ):
                    continue
                if query.statuses and task.status not in query.statuses:
                    continue
                if query.category_id is not None and task.category_id != query.category_id:
                    continue
                task_tag_ids = tag_ids_by_task.get(task_id, ())
                if query.tag_id is not None and query.tag_id not in task_tag_ids:
                    continue
                if not _matches_schedule_range(schedules, query.scheduled_from, query.scheduled_to):
                    continue
                views.append(_task_view(task, schedules, categories, tags, task_tag_ids))
            return _sort_task_views(views, query.sort_by, query.descending)

    def query_page(
        self,
        query: TaskQuery | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
        focus_task_id: int | None = None,
    ) -> TaskPage:
        with _service_errors():
            query = query or TaskQuery()
            if (
                query.scheduled_from is not None
                and query.scheduled_to is not None
                and query.scheduled_to < query.scheduled_from
            ):
                raise ServiceValidationError("日期區間的結束日期不可早於開始日期。")
            result = self.tasks.query_page(
                text=query.text,
                statuses=query.statuses,
                category_id=query.category_id,
                tag_id=query.tag_id,
                scheduled_from=query.scheduled_from,
                scheduled_to=query.scheduled_to,
                include_archived=query.include_archived,
                sort_by=query.sort_by.value,
                descending=query.descending,
                limit=limit,
                offset=offset,
                focus_task_id=focus_task_id,
            )
            schedules_by_task = _group_schedules(
                self.reviews.list_for_tasks(_task_ids(result.tasks))
            )
            tag_ids_by_task = self.tasks.tag_ids_by_task(_task_ids(result.tasks))
            categories, tags = _catalog_maps(self.catalog)
            views = tuple(
                _task_view(
                    task,
                    schedules_by_task.get(_persisted_id(task), ()),
                    categories,
                    tags,
                    tag_ids_by_task.get(_persisted_id(task), ()),
                )
                for task in result.tasks
            )
            return TaskPage(
                items=views,
                total_count=result.total_count,
                offset=result.offset,
                limit=limit,
            )

    def dashboard_summary(self) -> DashboardSummary:
        with _service_errors():
            today = self.now_provider().date()
            metrics = self.tasks.dashboard_metrics(today)
            return DashboardSummary(
                task_total=metrics.task_total,
                pending_review_count=metrics.pending_count,
                overdue_count=metrics.overdue_count,
                due_today_count=metrics.due_today_count,
                next_seven_days_count=metrics.next_seven_days_count,
                completed_review_count=metrics.completed_count,
                incomplete_review_count=metrics.pending_count + metrics.skipped_count,
                completion_rate=(
                    metrics.completed_count / metrics.schedule_total * 100.0
                    if metrics.schedule_total
                    else 0.0
                ),
            )

    def calendar(self, start_day: date, end_day: date) -> tuple[CalendarDaySummary, ...]:
        with _service_errors():
            if end_day < start_day:
                raise ServiceValidationError("月曆結束日期不可早於開始日期。")
            today = self.now_provider().date()
            metrics = {
                item.day: item
                for item in self.reviews.calendar_metrics(
                    start_day,
                    end_day,
                    today=today,
                )
            }
            results: list[CalendarDaySummary] = []
            current = start_day
            while current <= end_day:
                item = metrics.get(current)
                results.append(
                    CalendarDaySummary(
                        day=current,
                        schedule_count=item.schedule_count if item is not None else 0,
                        pending_count=item.pending_count if item is not None else 0,
                        completed_count=item.completed_count if item is not None else 0,
                        skipped_count=item.skipped_count if item is not None else 0,
                        overdue_count=item.overdue_count if item is not None else 0,
                    )
                )
                current += timedelta(days=1)
            return tuple(results)

    def categories(self) -> tuple[CatalogItem, ...]:
        with _service_errors():
            return tuple(CatalogItem(item.id, item.name) for item in self.catalog.list_categories())

    def tags(self) -> tuple[CatalogItem, ...]:
        with _service_errors():
            return tuple(CatalogItem(item.id, item.name) for item in self.catalog.list_tags())

    def create_category(self, name: str) -> CatalogItem:
        with _service_errors():
            normalized = name.strip()
            if not normalized:
                raise ServiceValidationError("分類名稱不可空白。")
            entry_id = self.catalog.create_category(normalized, now=self.now_provider())
            return CatalogItem(entry_id, normalized)

    def create_tag(self, name: str) -> CatalogItem:
        with _service_errors():
            normalized = name.strip()
            if not normalized:
                raise ServiceValidationError("標籤名稱不可空白。")
            entry_id = self.catalog.create_tag(normalized, now=self.now_provider())
            return CatalogItem(entry_id, normalized)

    def _schedule_times(self, draft: TaskDraft) -> tuple[datetime, ...]:
        if not draft.name.strip():
            raise ServiceValidationError("任務名稱不可空白。")
        if draft.schedule_mode is ScheduleMode.CURVE:
            if draft.review_count is None:
                raise ServiceValidationError("遺忘曲線模式必須設定複習次數。")
            if draft.manual_schedule_times:
                raise ServiceValidationError("遺忘曲線模式不可同時提供手動排程。")
            return generate_curve_schedule(draft.start_at, draft.review_count)
        if draft.schedule_mode is ScheduleMode.MANUAL:
            if draft.review_count is not None:
                raise ServiceValidationError("手動排程不可設定遺忘曲線複習次數。")
            return validate_manual_schedule(draft.start_at, draft.manual_schedule_times)
        raise ServiceValidationError("不支援的複習排程方式。")

    def _details_for(self, task: Task) -> TaskDetails:
        task_id = _persisted_id(task)
        schedules = self.reviews.list_for_task(task_id)
        categories, tags = _catalog_maps(self.catalog)
        tag_ids = self.tasks.tag_ids_for_task(task_id)
        return TaskDetails(
            task=_task_view(task, schedules, categories, tags, tag_ids),
            schedules=tuple(_schedule_view(item) for item in schedules),
        )


class ReviewService:
    def __init__(
        self,
        tasks: TaskRepository,
        reviews: ReviewRepository,
        catalog: CatalogRepository,
        *,
        now_provider: NowProvider = datetime.now,
    ) -> None:
        self.tasks = tasks
        self.reviews = reviews
        self.catalog = catalog
        self.now_provider = now_provider

    def due_today(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ReviewItem, ...]:
        with _service_errors():
            return self._review_items(
                self.reviews.list_due_today(
                    self.now_provider().date(),
                    limit=limit,
                    offset=offset,
                )
            )

    def overdue(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[ReviewItem, ...]:
        with _service_errors():
            return self._review_items(
                self.reviews.list_overdue(
                    self.now_provider().date(),
                    limit=limit,
                    offset=offset,
                )
            )

    def today_page(self, *, limit: int = 100, offset: int = 0) -> ReviewPage:
        with _service_errors():
            today = self.now_provider().date()
            overdue_count = self.reviews.count_overdue(today)
            today_count = self.reviews.count_due_today(today)
            schedules: list[ReviewSchedule] = []
            if offset < overdue_count:
                schedules.extend(
                    self.reviews.list_overdue(
                        today,
                        limit=limit,
                        offset=offset,
                    )
                )
            remaining = limit - len(schedules)
            today_offset = max(0, offset - overdue_count)
            if remaining:
                schedules.extend(
                    self.reviews.list_due_today(
                        today,
                        limit=remaining,
                        offset=today_offset,
                    )
                )
            return ReviewPage(
                items=self._review_items(schedules),
                total_count=overdue_count + today_count,
                today_count=today_count,
                overdue_count=overdue_count,
                offset=offset,
                limit=limit,
            )

    def next_seven_days(self) -> tuple[ReviewItem, ...]:
        with _service_errors():
            today = self.now_provider().date()
            schedules = self.reviews.list_active_between(today, today + timedelta(days=7))
            return self._review_items(
                item for item in schedules if item.status is ScheduleStatus.PENDING
            )

    def for_day(self, day: date) -> tuple[ReviewItem, ...]:
        with _service_errors():
            schedules = self.reviews.list_active_between(day, day + timedelta(days=1))
            return self._review_items(
                item for item in schedules if item.status is ScheduleStatus.PENDING
            )

    def day_page(self, day: date, *, limit: int = 100, offset: int = 0) -> ReviewPage:
        with _service_errors():
            total_count = self.reviews.count_pending_for_day(day)
            items = self._review_items(
                self.reviews.list_pending_for_day(day, limit=limit, offset=offset)
            )
            today = self.now_provider().date()
            return ReviewPage(
                items=items,
                total_count=total_count,
                today_count=total_count if day == today else 0,
                overdue_count=total_count if day < today else 0,
                offset=offset,
                limit=limit,
            )

    def complete(self, schedule_id: int) -> ReviewActionResult:
        with _service_errors():
            now = self.now_provider()
            schedule = self.reviews.complete(schedule_id, occurred_at=now, today=now.date())
            return self._action_result(schedule.task_id)

    def skip(self, schedule_id: int) -> ReviewActionResult:
        with _service_errors():
            now = self.now_provider()
            schedule = self.reviews.skip(schedule_id, occurred_at=now, today=now.date())
            return self._action_result(schedule.task_id)

    def postpone(self, schedule_id: int) -> ReviewActionResult:
        with _service_errors():
            now = self.now_provider()
            schedules = self.reviews.postpone(schedule_id, occurred_at=now, today=now.date())
            if not schedules:
                raise ServiceConflictError("推延後找不到所屬任務的排程。")
            return self._action_result(schedules[0].task_id)

    def reschedule(self, schedule_id: int, scheduled_at: datetime) -> ReviewActionResult:
        with _service_errors():
            now = self.now_provider()
            schedule = self.reviews.reschedule(
                schedule_id,
                scheduled_at=scheduled_at,
                today=now.date(),
                now=now,
            )
            return self._action_result(schedule.task_id)

    def _action_result(self, task_id: int) -> ReviewActionResult:
        task = self.tasks.get(task_id)
        schedules = self.reviews.list_for_task(task_id)
        return ReviewActionResult(
            task_id=task_id,
            task_status=task.status,
            completion_rate=task.completion_rate,
            schedules=tuple(_schedule_view(item) for item in schedules),
        )

    def _review_items(self, schedules: Iterable[ReviewSchedule]) -> tuple[ReviewItem, ...]:
        items = tuple(schedules)
        tasks = {
            task.id: task
            for task in self.tasks.list_by_ids(item.task_id for item in items)
            if task.id is not None
        }
        categories, tags = _catalog_maps(self.catalog)
        tag_ids_by_task = self.tasks.tag_ids_by_task(item.task_id for item in items)
        results: list[ReviewItem] = []
        for schedule in items:
            task = tasks.get(schedule.task_id)
            if task is None:
                raise ServiceConflictError("複習項目缺少所屬任務。")
            results.append(
                ReviewItem(
                    schedule_id=_persisted_schedule_id(schedule),
                    task_id=schedule.task_id,
                    task_name=task.name,
                    description=task.description,
                    category=categories.get(task.category_id),
                    tags=tuple(
                        tags[tag_id]
                        for tag_id in tag_ids_by_task.get(schedule.task_id, ())
                        if tag_id in tags
                    ),
                    sequence=schedule.sequence,
                    scheduled_at=schedule.scheduled_at,
                    status=schedule.status,
                    completion_rate=task.completion_rate,
                )
            )
        return tuple(results)


class SettingsService:
    def __init__(self, settings: SettingsRepository) -> None:
        self.settings = settings

    def get(self, key: str, default: object = None) -> object:
        normalized = key.strip()
        if not normalized:
            raise ServiceValidationError("設定鍵不可空白。")
        try:
            with _service_errors():
                return self.settings.get(normalized, default)
        except (TypeError, ValueError) as exc:
            raise ServiceConflictError("已儲存的設定格式無法讀取。") from exc

    def set(self, key: str, value: object, *, now: datetime | None = None) -> None:
        normalized = key.strip()
        if not normalized:
            raise ServiceValidationError("設定鍵不可空白。")
        try:
            with _service_errors():
                self.settings.set(normalized, value, now=now)
        except (TypeError, ValueError) as exc:
            raise ServiceValidationError("設定值必須是可儲存的 JSON 資料。") from exc


APPEARANCE_KEY = "appearance"
APPEARANCE_PAGE_IDS = frozenset({"reviews", "tasks", "calendar", "dashboard", "backup", "settings"})


class AssetService:
    def __init__(
        self,
        assets: AssetRepository,
        settings: SettingsRepository,
        store: AssetStore,
    ) -> None:
        self.assets = assets
        self.settings = settings
        self.store = store

    def list_assets(self, kind: str | None = None) -> tuple[AssetView, ...]:
        with _service_errors():
            return tuple(self._view(item) for item in self.assets.list(kind))

    def import_asset(self, source_path: str | Path, kind: str) -> AssetView:
        relative_path: str | None = None
        try:
            with _service_errors():
                relative_path = self.store.import_file(source_path, kind)
                try:
                    entry = self.assets.add(AssetEntry(kind=kind, relative_path=relative_path))
                except Exception:
                    try:
                        self.store.delete(relative_path)
                    except AssetStoreError:
                        pass
                    raise
                return self._view(entry)
        except AssetValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except AssetStorageError as exc:
            raise ServiceConflictError(str(exc)) from exc

    def appearance(self) -> AppearanceSettings:
        with _service_errors():
            raw = self.settings.get(APPEARANCE_KEY, {})
            parsed = _parse_appearance(raw)
            entries = self.assets.list()
        background_ids = {
            item.id for item in entries if item.id is not None and item.kind == "background"
        }
        sticker_ids = {
            item.id for item in entries if item.id is not None and item.kind == "sticker"
        }
        global_id = (
            parsed.global_background_id if parsed.global_background_id in background_ids else None
        )
        page_ids = {
            page_id: asset_id
            for page_id, asset_id in parsed.page_background_ids.items()
            if page_id in APPEARANCE_PAGE_IDS and asset_id in background_ids
        }
        sticker_id = parsed.sticker_asset_id if parsed.sticker_asset_id in sticker_ids else None
        return AppearanceSettings(
            background_mode=parsed.background_mode,
            global_background_id=global_id,
            page_background_ids=page_ids,
            sticker_asset_id=sticker_id,
            sticker_enabled=parsed.sticker_enabled and sticker_id is not None,
        )

    def configure_background(
        self,
        asset_id: int | None,
        *,
        mode: str,
        page_id: str | None = None,
    ) -> AppearanceSettings:
        if mode not in {"global", "page"}:
            raise ServiceValidationError("背景套用方式只支援全域或指定頁面。")
        if mode == "page" and page_id not in APPEARANCE_PAGE_IDS:
            raise ServiceValidationError("請選擇要套用背景的頁面。")
        if asset_id is not None:
            self._require_kind(asset_id, "background")
        current = self.appearance()
        pages = dict(current.page_background_ids)
        global_id = current.global_background_id
        if mode == "global":
            global_id = asset_id
        elif page_id is not None:
            if asset_id is None:
                pages.pop(page_id, None)
            else:
                pages[page_id] = asset_id
        updated = AppearanceSettings(
            background_mode=mode,
            global_background_id=global_id,
            page_background_ids=pages,
            sticker_asset_id=current.sticker_asset_id,
            sticker_enabled=current.sticker_enabled,
        )
        self._save_appearance(updated)
        return updated

    def configure_sticker(self, asset_id: int | None, *, enabled: bool) -> AppearanceSettings:
        if asset_id is not None:
            self._require_kind(asset_id, "sticker")
        if enabled and asset_id is None:
            raise ServiceValidationError("啟用貼圖前請先選擇貼圖。")
        current = self.appearance()
        updated = AppearanceSettings(
            background_mode=current.background_mode,
            global_background_id=current.global_background_id,
            page_background_ids=dict(current.page_background_ids),
            sticker_asset_id=asset_id,
            sticker_enabled=enabled and asset_id is not None,
        )
        self._save_appearance(updated)
        return updated

    def background_for(self, page_id: str) -> AssetView | None:
        with _service_errors():
            appearance = self.appearance()
            asset_id = (
                appearance.global_background_id
                if appearance.background_mode == "global"
                else appearance.page_background_ids.get(page_id)
            )
            return self._optional_view(asset_id)

    def sticker(self) -> AssetView | None:
        with _service_errors():
            appearance = self.appearance()
            if not appearance.sticker_enabled:
                return None
            return self._optional_view(appearance.sticker_asset_id)

    def delete_asset(self, asset_id: int) -> None:
        try:
            with _service_errors():
                entry = self.assets.get(asset_id)
                current = self.appearance()
                updated = AppearanceSettings(
                    background_mode=current.background_mode,
                    global_background_id=(
                        None
                        if current.global_background_id == asset_id
                        else current.global_background_id
                    ),
                    page_background_ids={
                        key: value
                        for key, value in current.page_background_ids.items()
                        if value != asset_id
                    },
                    sticker_asset_id=(
                        None if current.sticker_asset_id == asset_id else current.sticker_asset_id
                    ),
                    sticker_enabled=(
                        False if current.sticker_asset_id == asset_id else current.sticker_enabled
                    ),
                )
                staged = self.store.stage_delete(entry.relative_path)
                try:
                    self.assets.delete_with_setting(
                        asset_id,
                        APPEARANCE_KEY,
                        _appearance_payload(updated),
                    )
                except Exception:
                    self.store.restore_staged(entry.relative_path, staged)
                    raise
                self.store.finalize_staged(staged)
        except AssetValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except AssetStorageError as exc:
            raise ServiceConflictError(str(exc)) from exc

    def _require_kind(self, asset_id: int, kind: str) -> AssetEntry:
        with _service_errors():
            entry = self.assets.get(asset_id)
            if entry.kind != kind:
                raise ServiceValidationError("選取的素材類型不正確。")
            path = self.store.resolve(entry.relative_path)
            if not path.is_file():
                raise ServiceConflictError("找不到素材檔案，請重新上傳。")
            return entry

    def _optional_view(self, asset_id: int | None) -> AssetView | None:
        if asset_id is None:
            return None
        try:
            return self._view(self.assets.get(asset_id))
        except EntityNotFoundError:
            return None

    def _view(self, entry: AssetEntry) -> AssetView:
        if entry.id is None:
            raise ServiceConflictError("素材缺少識別碼。")
        path = self.store.resolve(entry.relative_path)
        return AssetView(
            id=entry.id,
            kind=entry.kind,
            relative_path=entry.relative_path,
            absolute_path=path,
            display_name=_asset_display_name(path),
            available=path.is_file(),
        )

    def _save_appearance(self, appearance: AppearanceSettings) -> None:
        try:
            with _service_errors():
                self.settings.set(APPEARANCE_KEY, _appearance_payload(appearance))
        except (TypeError, ValueError) as exc:
            raise ServiceValidationError("個人化設定無法儲存。") from exc


class BackupService:
    """Coordinates verified ZIP backups, restore points, and operation logs."""

    def __init__(
        self,
        archive: BackupArchive,
        logs: BackupLogRepository,
        *,
        now_provider: NowProvider = datetime.now,
    ) -> None:
        self.archive = archive
        self.logs = logs
        self.now_provider = now_provider

    def create_backup(self) -> BackupResult:
        now = self.now_provider()
        try:
            info = self.archive.create(now=now)
        except BackupArchiveError as exc:
            self._record(
                "backup",
                success=False,
                occurred_at=now,
                error_message=_backup_error_message(exc),
            )
            raise _translate_backup_error(exc) from exc
        self._record(
            "backup",
            success=True,
            occurred_at=now,
            backup_filename=info.path.name,
            backup_checksum=info.archive_sha256,
        )
        return BackupResult(
            path=info.path,
            created_at=info.created_at,
            archive_sha256=info.archive_sha256,
            file_count=len(info.files),
            size=info.path.stat().st_size,
        )

    def import_backup(self, source_path: str | Path) -> ImportResult:
        source = Path(source_path)
        now = self.now_provider()
        try:
            self.archive.validate(source)
        except BackupArchiveError as exc:
            self._record(
                "import",
                success=False,
                occurred_at=now,
                backup_filename=source.name,
                error_message=_backup_error_message(exc),
            )
            raise _translate_backup_error(exc) from exc

        try:
            restore = self.archive.create(
                now=now,
                filename_prefix="pre-import-restore",
            )
            self._record(
                "backup",
                success=True,
                occurred_at=now,
                backup_filename=restore.path.name,
                backup_checksum=restore.archive_sha256,
            )
        except BackupArchiveError as exc:
            self._record(
                "import",
                success=False,
                occurred_at=now,
                backup_filename=source.name,
                error_message="無法建立匯入前還原點。",
            )
            raise ServiceConflictError("無法建立匯入前還原點，尚未變更目前資料。") from exc

        try:
            installed = self.archive.install(source)
        except BackupArchiveError as exc:
            self._record(
                "import",
                success=False,
                occurred_at=now,
                backup_filename=source.name,
                error_message=_backup_error_message(exc),
            )
            raise _translate_backup_error(exc) from exc

        log_warning = None
        try:
            self._record(
                "import",
                success=True,
                occurred_at=now,
                backup_filename=source.name,
                backup_checksum=installed.archive_sha256,
            )
        except ServiceConflictError as exc:
            log_warning = str(exc)
        return ImportResult(
            source=source,
            restore_point=restore.path,
            imported_created_at=installed.created_at,
            warning=log_warning,
        )

    def _record(
        self,
        operation: str,
        *,
        success: bool,
        occurred_at: datetime,
        backup_filename: str | None = None,
        backup_checksum: str | None = None,
        error_message: str | None = None,
    ) -> None:
        try:
            self.logs.add(
                BackupLogEntry(
                    operation=operation,
                    occurred_at=occurred_at,
                    success=success,
                    backup_filename=backup_filename,
                    backup_checksum=backup_checksum,
                    error_message=error_message,
                )
            )
        except (RepositoryError, DatabaseError, sqlite3.Error) as exc:
            raise ServiceConflictError("備份操作結果無法寫入紀錄。") from exc


GMAIL_DEFAULT_RECIPIENT_KEY = "gmail.default_recipient"


class GmailService:
    """User-triggered Gmail authorization, draft preparation, and sending."""

    def __init__(
        self,
        *,
        backups: BackupService,
        reviews: ReviewService,
        settings: SettingsRepository,
        logs: BackupLogRepository,
        credentials: CredentialStore,
        oauth: OAuthGateway | None,
        gateway: GmailGateway,
        now_provider: NowProvider = datetime.now,
    ) -> None:
        self.backups = backups
        self.reviews = reviews
        self.settings = settings
        self.logs = logs
        self.credentials = credentials
        self.oauth = oauth
        self.gateway = gateway
        self.now_provider = now_provider

    def status(self) -> GmailAccountStatus:
        if self.oauth is None:
            return GmailAccountStatus(configured=False, linked=False)
        try:
            record = self.credentials.read()
        except CredentialStoreError as exc:
            raise ServiceConflictError(str(exc)) from exc
        return GmailAccountStatus(
            configured=True,
            linked=record is not None,
            account_email=record.account_email if record is not None else None,
        )

    def connect(self) -> GmailAccountStatus:
        oauth = self._configured_oauth()
        try:
            tokens = oauth.authorize()
            self._store_tokens(tokens)
        except GmailInfrastructureError as exc:
            raise _translate_gmail_error(exc) from exc
        return self.status()

    def reauthorize(self) -> GmailAccountStatus:
        return self.connect()

    def disconnect(self) -> GmailDisconnectResult:
        warning = None
        try:
            record = self.credentials.read() if self.oauth is not None else None
            if record is not None:
                try:
                    self.oauth.revoke(record.refresh_token)
                except GmailInfrastructureError:
                    warning = "本機授權已移除，但 Google 端授權暫時無法撤銷。"
            self.credentials.delete()
        except CredentialStoreError as exc:
            raise ServiceConflictError(str(exc)) from exc
        return GmailDisconnectResult(status=self.status(), warning=warning)

    def default_recipient(self) -> str:
        try:
            value = self.settings.get(GMAIL_DEFAULT_RECIPIENT_KEY, "")
        except (RepositoryError, DatabaseError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ServiceConflictError("無法讀取 Gmail 預設收件人。") from exc
        return value if isinstance(value, str) else ""

    def set_default_recipient(self, recipient: str) -> str:
        normalized = _normalize_recipient(recipient, allow_empty=True)
        try:
            self.settings.set(
                GMAIL_DEFAULT_RECIPIENT_KEY,
                normalized,
                now=self.now_provider(),
            )
        except (RepositoryError, DatabaseError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ServiceConflictError("無法保存 Gmail 預設收件人。") from exc
        return normalized

    def prepare_email(self) -> GmailDraft:
        self._configured_oauth()
        recipient = self.default_recipient()
        backup = self.backups.create_backup()
        now = self.now_provider()
        try:
            due_today = self.reviews.due_today()
            overdue = self.reviews.overdue()
        except ServiceError:
            raise
        subject = f"Task Assignment 備份｜{now:%Y-%m-%d}"
        body = _gmail_backup_body(
            created_at=backup.created_at,
            backup_filename=backup.path.name,
            due_today=due_today,
            overdue=overdue,
        )
        return GmailDraft(
            recipient=recipient,
            subject=subject,
            body=body,
            backup_path=backup.path,
            backup_filename=backup.path.name,
            backup_checksum=backup.archive_sha256,
            created_at=backup.created_at,
        )

    def send_prepared(
        self,
        draft: GmailDraft,
        *,
        recipient: str,
        subject: str,
        body: str,
    ) -> GmailSendResult:
        normalized_recipient = _normalize_recipient(recipient)
        normalized_subject = subject.strip()
        if not normalized_subject or len(normalized_subject) > 180:
            raise ServiceValidationError("郵件主旨不可空白，且不得超過 180 個字元。")
        if len(body) > 100_000:
            raise ServiceValidationError("郵件本文過長，請縮短後再寄送。")
        backup_path, checksum = self._verified_attachment(draft)
        now = self.now_provider()
        try:
            access_token, account_email = self._access_token()
            try:
                message_id = self.gateway.send(
                    access_token=access_token,
                    sender=account_email,
                    recipient=normalized_recipient,
                    subject=normalized_subject,
                    body=body,
                    attachment=backup_path,
                )
            except GmailAuthorizationError:
                self.credentials.delete()
                access_token, account_email = self._authorize_and_store()
                message_id = self.gateway.send(
                    access_token=access_token,
                    sender=account_email,
                    recipient=normalized_recipient,
                    subject=normalized_subject,
                    body=body,
                    attachment=backup_path,
                )
        except GmailInfrastructureError as exc:
            self._record_send(
                now=now,
                recipient=normalized_recipient,
                backup_filename=backup_path.name,
                checksum=checksum,
                success=False,
                error_message=_gmail_error_message(exc),
            )
            raise _translate_gmail_error(exc) from exc

        warning = None
        try:
            self._record_send(
                now=now,
                recipient=normalized_recipient,
                backup_filename=backup_path.name,
                checksum=checksum,
                success=True,
                message_id=message_id,
            )
        except ServiceConflictError as exc:
            warning = str(exc)
        return GmailSendResult(
            recipient=normalized_recipient,
            message_id=message_id,
            backup_path=backup_path,
            warning=warning,
        )

    def _configured_oauth(self) -> OAuthGateway:
        if self.oauth is None:
            raise ServiceConflictError(
                "此開發版本尚未配置發布者 Gmail OAuth；本機 ZIP 備份仍可正常使用。"
            )
        return self.oauth

    def _store_tokens(self, tokens: OAuthTokens) -> None:
        access_token = tokens.access_token
        refresh_token = tokens.refresh_token
        account_email = tokens.account_email
        if not isinstance(access_token, str) or not access_token.strip():
            raise GmailAuthorizationError("Google 未提供可用的 access token。")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise GmailAuthorizationError("Google 未提供可跨重啟使用的 refresh token。")
        if not isinstance(account_email, str) or not account_email.strip():
            raise GmailAuthorizationError("Google 未提供已連結的 Gmail 帳號。")
        try:
            normalized_account = _normalize_recipient(account_email)
        except ServiceValidationError as exc:
            raise GmailAuthorizationError("Google 回傳的 Gmail 帳號格式不正確。") from exc
        self.credentials.write(CredentialRecord(normalized_account, refresh_token))

    def _authorize_and_store(self) -> tuple[str, str]:
        tokens = self._configured_oauth().authorize()
        self._store_tokens(tokens)
        if tokens.account_email is None:
            raise GmailAuthorizationError("Google 未提供已連結的 Gmail 帳號。")
        return tokens.access_token, _normalize_recipient(tokens.account_email)

    def _access_token(self) -> tuple[str, str]:
        oauth = self._configured_oauth()
        record = self.credentials.read()
        if record is None:
            return self._authorize_and_store()
        try:
            tokens = oauth.refresh(record.refresh_token)
        except GmailReauthorizationRequired:
            self.credentials.delete()
            return self._authorize_and_store()
        if not isinstance(tokens.access_token, str) or not tokens.access_token.strip():
            raise GmailAuthorizationError("Google 未提供可用的 access token。")
        return tokens.access_token, record.account_email

    def _verified_attachment(self, draft: GmailDraft) -> tuple[Path, str]:
        try:
            backup_root = self.backups.archive.paths.backups.resolve()
            resolved = draft.backup_path.resolve(strict=True)
        except OSError as exc:
            raise ServiceValidationError("找不到準備寄送的本機備份 ZIP。") from exc
        if not resolved.is_relative_to(backup_root):
            raise ServiceValidationError("只能寄送 Task Assignment 本機備份目錄中的 ZIP。")
        try:
            validated = self.backups.archive.validate(resolved)
        except BackupArchiveError as exc:
            raise _translate_backup_error(exc) from exc
        if (
            resolved.name != draft.backup_filename
            or validated.archive_sha256 != draft.backup_checksum
        ):
            raise ServiceValidationError("準備寄送的備份 ZIP 已經變更，請重新建立。")
        return resolved, validated.archive_sha256

    def _record_send(
        self,
        *,
        now: datetime,
        recipient: str,
        backup_filename: str,
        checksum: str,
        success: bool,
        error_message: str | None = None,
        message_id: str | None = None,
    ) -> None:
        try:
            self.logs.add(
                BackupLogEntry(
                    operation="gmail_send",
                    occurred_at=now,
                    recipient=recipient,
                    backup_filename=backup_filename,
                    backup_checksum=checksum,
                    success=success,
                    error_message=error_message,
                    gmail_message_id=message_id,
                )
            )
        except (RepositoryError, DatabaseError, sqlite3.Error) as exc:
            raise ServiceConflictError("Gmail 寄送結果無法寫入紀錄。") from exc


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    tasks: TaskService
    reviews: ReviewService
    settings: SettingsService
    assets: AssetService
    backups: BackupService
    gmail: GmailService


def create_services(
    database: Database,
    *,
    now_provider: NowProvider = datetime.now,
    paths: AppPaths | None = None,
    credential_store: CredentialStore | None = None,
    oauth_gateway: OAuthGateway | None = None,
    gmail_gateway: GmailGateway | None = None,
    oauth_config: OAuthClientConfig | None = None,
) -> ApplicationServices:
    tasks = TaskRepository(database)
    reviews = ReviewRepository(database)
    catalog = CatalogRepository(database)
    settings = SettingsRepository(database)
    asset_paths = paths or AppPaths.from_base_dir(database.path.parent)
    asset_paths.ensure_directories()
    backup_logs = BackupLogRepository(database)
    backup_service = BackupService(
        BackupArchive(database, asset_paths),
        backup_logs,
        now_provider=now_provider,
    )
    configured_oauth = oauth_gateway
    if configured_oauth is None:
        loaded_config = oauth_config or OAuthClientConfig.load()
        if loaded_config is not None:
            configured_oauth = GoogleOAuthClient(loaded_config)
    gmail_service = GmailService(
        backups=backup_service,
        reviews=ReviewService(tasks, reviews, catalog, now_provider=now_provider),
        settings=settings,
        logs=backup_logs,
        credentials=credential_store or WindowsCredentialStore(),
        oauth=configured_oauth,
        gateway=gmail_gateway or GmailApiClient(),
        now_provider=now_provider,
    )
    return ApplicationServices(
        tasks=TaskService(tasks, reviews, catalog, now_provider=now_provider),
        reviews=gmail_service.reviews,
        settings=SettingsService(settings),
        assets=AssetService(AssetRepository(database), settings, AssetStore(asset_paths)),
        backups=backup_service,
        gmail=gmail_service,
    )


def _normalize_recipient(value: str, *, allow_empty: bool = False) -> str:
    normalized = value.strip()
    if allow_empty and not normalized:
        return ""
    if not normalized or len(normalized) > 254 or "\r" in normalized or "\n" in normalized:
        raise ServiceValidationError("請輸入有效的 Gmail 收件人地址。")
    try:
        address = Address(addr_spec=normalized)
    except ValueError as exc:
        raise ServiceValidationError("請輸入有效的 Gmail 收件人地址。") from exc
    if not address.username or not address.domain:
        raise ServiceValidationError("請輸入有效的 Gmail 收件人地址。")
    return address.addr_spec


def _gmail_backup_body(
    *,
    created_at: datetime,
    backup_filename: str,
    due_today: tuple[ReviewItem, ...],
    overdue: tuple[ReviewItem, ...],
) -> str:
    lines = [
        "Task Assignment 本機完整備份",
        "",
        f"建立時間：{created_at:%Y-%m-%d %H:%M}",
        f"備份檔案：{backup_filename}",
        "完整資料請以隨信附上的 ZIP 為準。",
        "",
        f"今日待複習（{len(due_today)}）",
    ]
    lines.extend(_review_email_line(item, "今日待複習") for item in due_today)
    if not due_today:
        lines.append("- 無")
    lines.extend(("", f"逾期未處理（{len(overdue)}）"))
    lines.extend(_review_email_line(item, "逾期") for item in overdue)
    if not overdue:
        lines.append("- 無")
    body = "\n".join(lines)
    if len(body) > 95_000:
        body = body[:94_900].rstrip() + "\n\n（項目過多，本文已截短；完整資料請見 ZIP。）"
    return body


def _review_email_line(item: ReviewItem, status: str) -> str:
    category = item.category.name if item.category is not None else "未分類"
    tags = "、".join(tag.name for tag in item.tags) or "無標籤"
    description = " ".join(item.description.split()) or "無說明"
    return (
        f"- {item.task_name}｜{item.scheduled_at:%Y-%m-%d %H:%M}｜{status}｜"
        f"分類：{category}｜標籤：{tags}｜說明：{description}"
    )


def _translate_gmail_error(error: GmailInfrastructureError) -> ServiceError:
    if isinstance(error, GmailMessageError):
        return ServiceValidationError(str(error))
    if isinstance(
        error,
        (
            GmailAuthorizationError,
            GmailConfigurationError,
            GmailNetworkError,
            CredentialStoreError,
        ),
    ):
        return ServiceConflictError(str(error))
    return ServiceConflictError("Gmail 操作失敗，本機備份不會被刪除。")


def _gmail_error_message(error: GmailInfrastructureError) -> str:
    if isinstance(
        error,
        (
            GmailAuthorizationError,
            GmailConfigurationError,
            GmailMessageError,
            GmailNetworkError,
            CredentialStoreError,
        ),
    ):
        return str(error)
    return "Gmail 操作失敗，本機備份已保留。"


def _translate_backup_error(error: BackupArchiveError) -> ServiceError:
    message = _backup_error_message(error)
    if isinstance(error, BackupArchiveValidationError):
        return ServiceValidationError(message)
    return ServiceConflictError(message)


def _backup_error_message(error: BackupArchiveError) -> str:
    if isinstance(error, BackupArchiveValidationError):
        return str(error)
    if isinstance(error, BackupArchiveStorageError):
        return str(error)
    return "備份操作失敗，尚未完成任何變更。"


@contextmanager
def _service_errors() -> Iterator[None]:
    try:
        yield
    except ServiceError:
        raise
    except EntityNotFoundError as exc:
        raise ServiceNotFoundError(str(exc)) from exc
    except DomainError as exc:
        raise ServiceValidationError(str(exc)) from exc
    except AssetValidationError as exc:
        raise ServiceValidationError(str(exc)) from exc
    except AssetStorageError as exc:
        raise ServiceConflictError(str(exc)) from exc
    except RepositoryError as exc:
        raise ServiceConflictError(str(exc)) from exc
    except (DatabaseError, sqlite3.Error) as exc:
        raise ServiceConflictError("無法讀取或儲存資料，請稍後再試。") from exc


def _parse_appearance(raw: object) -> AppearanceSettings:
    if raw in (None, {}):
        return AppearanceSettings()
    if not isinstance(raw, dict):
        raise ServiceConflictError("已儲存的個人化設定格式無法讀取。")
    mode = raw.get("background_mode", "global")
    if mode not in {"global", "page"}:
        mode = "global"
    page_values = raw.get("page_background_ids", {})
    pages = (
        {
            str(key): value
            for key, value in page_values.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if isinstance(page_values, dict)
        else {}
    )
    return AppearanceSettings(
        background_mode=str(mode),
        global_background_id=_optional_int(raw.get("global_background_id")),
        page_background_ids=pages,
        sticker_asset_id=_optional_int(raw.get("sticker_asset_id")),
        sticker_enabled=raw.get("sticker_enabled") is True,
    )


def _appearance_payload(value: AppearanceSettings) -> dict[str, object]:
    return {
        "background_mode": value.background_mode,
        "global_background_id": value.global_background_id,
        "page_background_ids": dict(value.page_background_ids),
        "sticker_asset_id": value.sticker_asset_id,
        "sticker_enabled": value.sticker_enabled,
    }


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _asset_display_name(path: Path) -> str:
    stem, separator, token = path.stem.rpartition("-")
    display_stem = stem if separator and len(token) == 12 else path.stem
    return f"{display_stem}{path.suffix.lower()}"


def _catalog_maps(
    catalog: CatalogRepository,
) -> tuple[dict[int | None, CatalogItem], dict[int, CatalogItem]]:
    categories = {item.id: CatalogItem(item.id, item.name) for item in catalog.list_categories()}
    tags = {item.id: CatalogItem(item.id, item.name) for item in catalog.list_tags()}
    return categories, tags


def _task_ids(tasks: Iterable[Task]) -> tuple[int, ...]:
    return tuple(_persisted_id(task) for task in tasks)


def _persisted_id(task: Task) -> int:
    if task.id is None:
        raise ServiceConflictError("資料庫任務缺少 ID。")
    return task.id


def _persisted_schedule_id(schedule: ReviewSchedule) -> int:
    if schedule.id is None:
        raise ServiceConflictError("資料庫複習項目缺少 ID。")
    return schedule.id


def _group_schedules(
    schedules: Iterable[ReviewSchedule],
) -> dict[int, tuple[ReviewSchedule, ...]]:
    grouped: dict[int, list[ReviewSchedule]] = defaultdict(list)
    for schedule in schedules:
        grouped[schedule.task_id].append(schedule)
    return {task_id: tuple(items) for task_id, items in grouped.items()}


def _schedule_view(schedule: ReviewSchedule) -> ScheduleView:
    return ScheduleView(
        id=_persisted_schedule_id(schedule),
        sequence=schedule.sequence,
        scheduled_at=schedule.scheduled_at,
        status=schedule.status,
        processed_at=schedule.processed_at,
    )


def _task_view(
    task: Task,
    schedules: Iterable[ReviewSchedule],
    categories: dict[int | None, CatalogItem],
    tags: dict[int, CatalogItem],
    task_tag_ids: Iterable[int],
) -> TaskView:
    items = tuple(schedules)
    pending_times = tuple(
        item.scheduled_at for item in items if item.status is ScheduleStatus.PENDING
    )
    if task.created_at is None or task.updated_at is None:
        raise ServiceConflictError("資料庫任務缺少建立或修改時間。")
    return TaskView(
        id=_persisted_id(task),
        name=task.name,
        description=task.description,
        category=categories.get(task.category_id),
        tags=tuple(tags[tag_id] for tag_id in task_tag_ids if tag_id in tags),
        start_at=task.start_at,
        schedule_mode=task.schedule_mode,
        review_count=task.review_count,
        status=task.status,
        completion_rate=task.completion_rate,
        is_paused=task.is_paused,
        paused_on=task.paused_on,
        is_archived=task.is_archived,
        created_at=task.created_at,
        updated_at=task.updated_at,
        next_review_at=min(pending_times) if pending_times else None,
    )


def _matches_schedule_range(
    schedules: Iterable[ReviewSchedule], start_day: date | None, end_day: date | None
) -> bool:
    if start_day is None and end_day is None:
        return True
    return any(
        (start_day is None or schedule.scheduled_at.date() >= start_day)
        and (end_day is None or schedule.scheduled_at.date() <= end_day)
        for schedule in schedules
    )


def _sort_task_views(
    views: Iterable[TaskView], sort_by: TaskSort, descending: bool
) -> tuple[TaskView, ...]:
    items = tuple(views)
    if sort_by is TaskSort.NAME:
        return tuple(
            sorted(items, key=lambda item: (item.name.casefold(), item.id), reverse=descending)
        )
    if sort_by is TaskSort.CREATED_AT:
        return tuple(sorted(items, key=lambda item: (item.created_at, item.id), reverse=descending))
    if sort_by is TaskSort.NEXT_REVIEW:
        scheduled = [item for item in items if item.next_review_at is not None]
        unscheduled = [item for item in items if item.next_review_at is None]
        scheduled.sort(key=lambda item: (item.next_review_at, item.id), reverse=descending)
        unscheduled.sort(key=lambda item: item.id)
        return tuple((*scheduled, *unscheduled))
    raise ServiceValidationError("不支援的任務排序方式。")
