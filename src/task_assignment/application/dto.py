"""Stable DTOs returned to the presentation layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

from task_assignment.domain.enums import ScheduleMode, ScheduleStatus, TaskStatus


class TaskSort(StrEnum):
    NAME = "name"
    CREATED_AT = "created_at"
    NEXT_REVIEW = "next_review"


@dataclass(frozen=True, slots=True)
class TaskDraft:
    name: str
    start_at: datetime
    schedule_mode: ScheduleMode
    description: str = ""
    category_id: int | None = None
    tag_ids: tuple[int, ...] = ()
    review_count: int | None = None
    manual_schedule_times: tuple[datetime, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskQuery:
    text: str = ""
    statuses: frozenset[TaskStatus] = field(default_factory=frozenset)
    category_id: int | None = None
    tag_id: int | None = None
    scheduled_from: date | None = None
    scheduled_to: date | None = None
    include_archived: bool = False
    sort_by: TaskSort = TaskSort.CREATED_AT
    descending: bool = False


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class ScheduleView:
    id: int
    sequence: int
    scheduled_at: datetime
    status: ScheduleStatus
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskView:
    id: int
    name: str
    description: str
    category: CatalogItem | None
    tags: tuple[CatalogItem, ...]
    start_at: datetime
    schedule_mode: ScheduleMode
    review_count: int | None
    status: TaskStatus
    completion_rate: float
    is_paused: bool
    paused_on: date | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    next_review_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskPage:
    items: tuple[TaskView, ...]
    total_count: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class TaskDetails:
    task: TaskView
    schedules: tuple[ScheduleView, ...]


@dataclass(frozen=True, slots=True)
class SchedulePreview:
    scheduled_at: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class ScheduleChangePreview:
    preserved_history: tuple[ScheduleView, ...]
    unchanged_pending: tuple[datetime, ...]
    removed_pending: tuple[datetime, ...]
    added_pending: tuple[datetime, ...]


@dataclass(frozen=True, slots=True)
class ReviewItem:
    schedule_id: int
    task_id: int
    task_name: str
    description: str
    category: CatalogItem | None
    tags: tuple[CatalogItem, ...]
    sequence: int
    scheduled_at: datetime
    status: ScheduleStatus
    completion_rate: float


@dataclass(frozen=True, slots=True)
class ReviewPage:
    items: tuple[ReviewItem, ...]
    total_count: int
    today_count: int
    overdue_count: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class ReviewActionResult:
    task_id: int
    task_status: TaskStatus
    completion_rate: float
    schedules: tuple[ScheduleView, ...]


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    task_total: int
    pending_review_count: int
    overdue_count: int
    due_today_count: int
    next_seven_days_count: int
    completed_review_count: int
    incomplete_review_count: int
    completion_rate: float


@dataclass(frozen=True, slots=True)
class CalendarDaySummary:
    day: date
    schedule_count: int
    pending_count: int
    completed_count: int
    skipped_count: int
    overdue_count: int


@dataclass(frozen=True, slots=True)
class AssetView:
    id: int
    kind: str
    relative_path: str
    absolute_path: Path
    display_name: str
    available: bool


@dataclass(frozen=True, slots=True)
class AppearanceSettings:
    background_mode: str = "global"
    global_background_id: int | None = None
    page_background_ids: dict[str, int] = field(default_factory=dict)
    sticker_asset_id: int | None = None
    sticker_enabled: bool = False


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    created_at: datetime
    archive_sha256: str
    file_count: int
    size: int


@dataclass(frozen=True, slots=True)
class ImportResult:
    source: Path
    restore_point: Path
    imported_created_at: datetime
    restart_required: bool = True
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class GmailAccountStatus:
    configured: bool
    linked: bool
    account_email: str | None = None


@dataclass(frozen=True, slots=True)
class GmailDraft:
    recipient: str
    subject: str
    body: str
    backup_path: Path
    backup_filename: str
    backup_checksum: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GmailSendResult:
    recipient: str
    message_id: str
    backup_path: Path
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class GmailDisconnectResult:
    status: GmailAccountStatus
    warning: str | None = None
