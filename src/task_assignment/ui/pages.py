"""Primary Task Assignment pages backed by application services."""

from __future__ import annotations

import calendar as calendar_module
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QDate,
    QEvent,
    QModelIndex,
    QObject,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    QVariantAnimation,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from task_assignment.application import (
    ApplicationServices,
    AssetView,
    BackupResult,
    CalendarDaySummary,
    DashboardSummary,
    GmailAccountStatus,
    GmailDisconnectResult,
    GmailDraft,
    GmailSendResult,
    ImportResult,
    ReviewItem,
    ServiceError,
    TaskQuery,
    TaskSort,
    TaskView,
)
from task_assignment.domain.enums import TaskStatus
from task_assignment.ui.task_dialog import DateTimeDialog, TaskDetailsDialog, TaskDialog
from task_assignment.ui.visuals import scaled
from task_assignment.version import APP_VERSION, RELEASE_UPDATED_AT, VERSION_NUMBER

STATUS_LABELS = {
    TaskStatus.NOT_STARTED: "未開始",
    TaskStatus.IN_PROGRESS: "進行中",
    TaskStatus.DUE_TODAY: "今日待複習",
    TaskStatus.OVERDUE: "逾期",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.INCOMPLETE: "未完全完成",
    TaskStatus.PAUSED: "已暫停",
    TaskStatus.ARCHIVED: "已封存",
}
DATE_TIME_FORMAT = "%Y-%m-%d %H:%M"
TABLE_PAGE_SIZE = 100


class _OperationWorker(QObject):
    succeeded = Signal(object)
    failed = Signal(object)
    finished = Signal()

    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.operation = operation

    @Slot()
    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as exc:  # The UI boundary converts unexpected failures to a safe message.
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class GmailDraftDialog(QDialog):
    def __init__(self, draft: GmailDraft, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("確認 Gmail 備份郵件")
        self.setMinimumSize(560, 420)
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        heading = QLabel("確認後才會開啟 Gmail 授權並寄送")
        heading.setObjectName("dialogTitle")
        layout.addWidget(heading)
        backup_label = QLabel(
            f"附件：{draft.backup_filename}\nZIP 已保留在本機；取消或寄送失敗都不會刪除。"
        )
        backup_label.setWordWrap(True)
        layout.addWidget(backup_label)
        form = QFormLayout()
        self.recipient_edit = QLineEdit(draft.recipient)
        self.recipient_edit.setPlaceholderText("例如 name@example.com")
        self.subject_edit = QLineEdit(draft.subject)
        form.addRow("收件人", self.recipient_edit)
        form.addRow("主旨", self.subject_edit)
        layout.addLayout(form)
        self.body_edit = QTextEdit(draft.body)
        self.body_edit.setAcceptRichText(False)
        layout.addWidget(QLabel("郵件本文"))
        layout.addWidget(self.body_edit, 1)
        self.save_default_check = QCheckBox("將這個地址保存為預設收件人")
        layout.addWidget(self.save_default_check)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("確認並寄送")
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    def recipient(self) -> str:
        return self.recipient_edit.text()

    def subject(self) -> str:
        return self.subject_edit.text()

    def body(self) -> str:
        return self.body_edit.toPlainText()


class PageBase(QWidget):
    data_changed = Signal()

    def _begin_operation(self, kind: str) -> bool:
        coordinator = getattr(self.window(), "try_start_operation", None)
        return coordinator(self, kind) if coordinator is not None else True

    def _end_operation(self) -> None:
        coordinator = getattr(self.window(), "finish_operation", None)
        if coordinator is not None:
            coordinator(self)

    def __init__(
        self,
        title: str,
        description: str,
        help_text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("contentPage")
        self.page_title = title
        self.help_text = help_text
        self.background_path: Path | None = None
        self._background_pixmap = QPixmap()
        self.root_layout = QVBoxLayout(self)
        self.root_layout.setContentsMargins(28, 24, 28, 24)
        self.root_layout.setSpacing(14)
        heading_row = QHBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        heading.setWordWrap(True)
        heading_row.addWidget(heading)
        heading_row.addStretch(1)
        self.help_button = QToolButton()
        self.help_button.setObjectName("pageHelpButton")
        self.help_button.setText("?")
        self.help_button.setToolTip(f"查看「{title}」操作說明")
        self.help_button.setAccessibleName(f"開啟{title}操作說明")
        self.help_button.clicked.connect(self._show_help)
        heading_row.addWidget(self.help_button, alignment=Qt.AlignmentFlag.AlignTop)
        self.root_layout.addLayout(heading_row)
        description_label = QLabel(description)
        description_label.setObjectName("pageDescription")
        description_label.setWordWrap(True)
        self.root_layout.addWidget(description_label)

    def refresh(self) -> None:
        return

    def _show_error(self, title: str, error: Exception) -> None:
        QMessageBox.warning(self, title, str(error))

    def _show_help(self) -> None:
        QMessageBox.information(self, f"{self.page_title}操作說明", self.help_text)

    def set_background(self, path: Path | None) -> None:
        self.background_path = path
        self._background_pixmap = QPixmap(str(path)) if path is not None else QPixmap()
        self.update()

    def paintEvent(self, event: QEvent) -> None:
        super().paintEvent(event)
        if self._background_pixmap.isNull():
            return
        painter = QPainter(self)
        scaled = self._background_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        source_x = max(0, (scaled.width() - self.width()) // 2)
        source_y = max(0, (scaled.height() - self.height()) // 2)
        painter.drawPixmap(
            self.rect(), scaled, QRect(source_x, source_y, self.width(), self.height())
        )
        painter.fillRect(self.rect(), QColor(247, 247, 244, 155))


class _ReviewSelectableCell(QFrame):
    """Selectable task cell used by the three-zone review row."""

    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


class _ReviewNameCell(_ReviewSelectableCell):
    def __init__(self, item: ReviewItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("reviewNameCell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(4)
        name = QLabel(item.task_name)
        name.setObjectName("reviewTaskName")
        name.setWordWrap(True)
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        metadata = QLabel(
            " · ".join(
                value
                for value in (
                    item.scheduled_at.strftime(DATE_TIME_FORMAT),
                    f"第 {item.sequence} 次",
                    item.category.name if item.category else "未分類",
                )
                if value
            )
        )
        metadata.setObjectName("reviewTaskMetadata")
        metadata.setWordWrap(True)
        metadata.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(name)
        layout.addWidget(metadata)
        layout.addStretch(1)


class _ReviewNoteCell(_ReviewSelectableCell):
    def __init__(self, item: ReviewItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("reviewNoteCell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        heading = QLabel("備註")
        heading.setObjectName("reviewNoteHeading")
        heading.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        note = QLabel(item.description or "無說明／筆記")
        note.setObjectName("reviewTaskNote")
        note.setWordWrap(True)
        note.setToolTip(item.description or "無說明／筆記")
        note.setMaximumHeight(scaled(64))
        note.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(heading)
        layout.addWidget(note)
        layout.addStretch(1)


class _ReviewActionCell(QFrame):
    complete_requested = Signal()
    postpone_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("todayTaskActionCell")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 12, 10)
        layout.setSpacing(6)
        self.complete_button = QPushButton("完成")
        self.complete_button.setObjectName("inlineCompleteButton")
        self.postpone_button = QPushButton("推到明天")
        self.postpone_button.setObjectName("inlinePostponeButton")
        self.complete_button.clicked.connect(self.complete_requested)
        self.postpone_button.clicked.connect(self.postpone_requested)
        layout.addWidget(self.complete_button)
        layout.addWidget(self.postpone_button)
        layout.addStretch(1)


class TodayTasksPage(PageBase):
    manage_task_requested = Signal(int)

    def __init__(self, services: ApplicationServices, parent: QWidget | None = None) -> None:
        super().__init__(
            "今日任務",
            "留一點時間給今天，讓每一次複習都成為進步。",
            (
                "先選擇一張任務卡片，再使用下方按鈕完成、推到明天、略過或修改時間。\n\n"
                "紅色『逾期』代表排程日期早於今天；排在今天較早時間的項目仍屬於今日。\n\n"
                "『管理任務』會帶你到該任務的編輯、暫停、封存與刪除操作。"
            ),
            parent,
        )
        self.services = services
        self.items: list[ReviewItem] = []
        self._page_index = 0

        from task_assignment.ui.learning_visuals import LearningHero

        self.hero = LearningHero(self)
        self.root_layout.addWidget(self.hero)

        summary = QHBoxLayout()
        self.today_count = _metric_chip("今日 0")
        self.overdue_count = _metric_chip("逾期 0", accent="danger")
        summary.addWidget(self.today_count)
        summary.addWidget(self.overdue_count)
        summary.addStretch(1)
        refresh_button = QPushButton("重新整理")
        refresh_button.setObjectName("secondaryButton")
        refresh_button.clicked.connect(self.refresh)
        summary.addWidget(refresh_button)
        self.root_layout.addLayout(summary)

        self.table = _table(("類型", "任務名稱", "備註", "處理"))
        self.table.setObjectName("todayTasksTable")
        self.table.horizontalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(False)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setMinimumHeight(150)
        self.table.setColumnHidden(0, True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.root_layout.addWidget(self.table, 1)

        pagination = QHBoxLayout()
        self.previous_page_button = QPushButton("上一頁")
        self.page_label = QLabel()
        self.page_label.setObjectName("paginationLabel")
        self.next_page_button = QPushButton("下一頁")
        pagination.addStretch(1)
        pagination.addWidget(self.previous_page_button)
        pagination.addWidget(self.page_label)
        pagination.addWidget(self.next_page_button)
        self.root_layout.addLayout(pagination)

        actions = QHBoxLayout()
        self.complete_button = QPushButton("完成")
        self.complete_button.setObjectName("primaryButton")
        self.postpone_button = QPushButton("推到明天")
        self.skip_button = QPushButton("略過")
        self.edit_button = QPushButton("編輯時間")
        self.detail_button = QPushButton("詳細資料")
        self.manage_button = QPushButton("管理任務")
        # Kept as non-visible compatibility actions for keyboard/tests; visible
        # completion actions now live on each task row.
        self.complete_button.hide()
        self.postpone_button.hide()
        for button in (self.skip_button, self.edit_button, self.detail_button, self.manage_button):
            actions.addWidget(button)
        actions.addStretch(1)
        self.root_layout.addLayout(actions)
        self.complete_button.clicked.connect(self._complete)
        self.postpone_button.clicked.connect(self._postpone)
        self.skip_button.clicked.connect(self._skip)
        self.edit_button.clicked.connect(self._edit_time)
        self.detail_button.clicked.connect(self._show_details)
        self.manage_button.clicked.connect(self._manage_task)
        self.previous_page_button.clicked.connect(lambda: self._change_page(-1))
        self.next_page_button.clicked.connect(lambda: self._change_page(1))
        self._update_actions()
        self.refresh()

    def refresh(self) -> None:
        try:
            page = self.services.reviews.today_page(
                limit=TABLE_PAGE_SIZE,
                offset=self._page_index * TABLE_PAGE_SIZE,
            )
            last_page = max(0, (page.total_count - 1) // TABLE_PAGE_SIZE)
            if self._page_index > last_page:
                self._page_index = last_page
                page = self.services.reviews.today_page(
                    limit=TABLE_PAGE_SIZE,
                    offset=self._page_index * TABLE_PAGE_SIZE,
                )
        except ServiceError as exc:
            self._show_error("無法載入今日任務", exc)
            return
        from task_assignment.ui.learning_visuals import CardDeparture

        CardDeparture.cancel(self.table)
        self.items = list(page.items)
        self.today_count.setText(f"今日 {page.today_count}")
        self.overdue_count.setText(f"逾期 {page.overdue_count}")
        self.hero.set_counts(page.today_count, page.overdue_count)
        self._update_pagination(page.total_count)
        self.table.setRowCount(len(self.items))
        today_date = self.services.reviews.now_provider().date()
        for row, item in enumerate(self.items):
            kind = "逾期" if item.scheduled_at.date() < today_date else "今日"
            category_and_tags = " · ".join(
                value
                for value in (
                    item.category.name if item.category else "未分類",
                    ", ".join(tag.name for tag in item.tags),
                )
                if value
            )
            metadata = " · ".join(
                value
                for value in (
                    kind,
                    item.scheduled_at.strftime(DATE_TIME_FORMAT),
                    f"第 {item.sequence} 次",
                    category_and_tags,
                    f"完成率 {item.completion_rate:.0f}%",
                )
                if value
            )
            self.table.setItem(row, 0, QTableWidgetItem(kind))
            name_item = QTableWidgetItem(item.task_name)
            name_item.setToolTip(metadata)
            name_item.setData(
                Qt.ItemDataRole.AccessibleTextRole,
                f"{item.task_name}，{metadata}，備註：{item.description or '無'}",
            )
            self.table.setItem(row, 1, name_item)
            note_item = QTableWidgetItem(item.description or "無說明／筆記")
            note_item.setToolTip(item.description or "無說明／筆記")
            self.table.setItem(row, 2, note_item)
            self.table.setItem(row, 3, QTableWidgetItem("完成／推到明天"))

            name_cell = _ReviewNameCell(item)
            name_cell.clicked.connect(lambda row=row: self._select_row(row))
            note_cell = _ReviewNoteCell(item)
            note_cell.clicked.connect(lambda row=row: self._select_row(row))
            action_cell = _ReviewActionCell()
            action_cell.complete_requested.connect(lambda row=row: self._complete_row(row))
            action_cell.postpone_requested.connect(lambda row=row: self._postpone_row(row))
            self.table.setCellWidget(row, 1, name_cell)
            self.table.setCellWidget(row, 2, note_cell)
            self.table.setCellWidget(row, 3, action_cell)
            self.table.setRowHeight(row, max(scaled(96), action_cell.sizeHint().height()))
        self._update_actions()

    def _change_page(self, step: int) -> None:
        target = self._page_index + step
        if target < 0:
            return
        self._page_index = target
        self.refresh()

    def _update_pagination(self, total_count: int) -> None:
        page_count = (total_count + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE
        current = self._page_index + 1 if total_count else 0
        self.page_label.setText(f"第 {current} / {page_count} 頁｜共 {total_count} 筆")
        self.previous_page_button.setEnabled(self._page_index > 0)
        self.next_page_button.setEnabled(self._page_index + 1 < page_count)

    def _selected(self) -> ReviewItem | None:
        row = self.table.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _select_row(self, row: int) -> None:
        if 0 <= row < len(self.items):
            self.table.selectRow(row)

    def _complete_row(self, row: int) -> None:
        self._select_row(row)
        self._complete()

    def _postpone_row(self, row: int) -> None:
        self._select_row(row)
        self._postpone()

    def _update_actions(self) -> None:
        enabled = self._selected() is not None
        for button in (
            self.complete_button,
            self.postpone_button,
            self.skip_button,
            self.edit_button,
            self.detail_button,
            self.manage_button,
        ):
            button.setEnabled(enabled)

    def _complete(self) -> None:
        item = self._selected()
        if item is not None:
            from task_assignment.ui.learning_visuals import (
                CardDeparture,
                CompletionFeedback,
                motion_mode,
            )

            CardDeparture.cancel(self.table)
            for feedback in self.table.viewport().findChildren(CompletionFeedback):
                feedback.hide()
                feedback.deleteLater()
            mode = motion_mode(self)
            row_rect = self.table.visualRect(self.table.currentIndex())
            scroll = self.table.verticalScrollBar().value()
            page_index = self._page_index
            before = (
                self.table.viewport().grab()
                if mode != "off" and self.table.isVisible() else None
            )

            try:
                result = self.services.reviews.complete(item.schedule_id)
            except ServiceError as exc:
                self._show_error("無法完成複習", exc)
                return
            self.refresh()
            self.data_changed.emit()
            if before is not None and row_rect.intersects(self.table.viewport().rect()):
                departure_mode = mode if (
                    scroll == self.table.verticalScrollBar().value()
                    and page_index == self._page_index
                ) else "reduced"
                CardDeparture(self.table, before, row_rect, departure_mode)
            for feedback in self.table.viewport().findChildren(CompletionFeedback):
                feedback.hide()
                feedback.deleteLater()
            CompletionFeedback(
                self.table.viewport(),
                "這項任務的複習已全部完成！"
                if result.completion_rate == 100
                else "這次複習已完成，繼續累積！",
                motion_mode(self),
                (item.completion_rate, result.completion_rate),
            )

    def _postpone(self) -> None:
        item = self._selected()
        if item is None:
            return
        if (
            QMessageBox.question(
                self,
                "推到明天",
                "目前項目與同任務後續待處理排程都會順延一天，確定繼續嗎？",
            )
            is QMessageBox.StandardButton.Yes
        ):
            self._mutate("無法推延複習", self.services.reviews.postpone, item.schedule_id)

    def _skip(self) -> None:
        item = self._selected()
        if item is None:
            return
        if (
            QMessageBox.question(
                self,
                "略過複習",
                "略過後不可復原，且仍會計入完成率分母。確定略過嗎？",
            )
            is QMessageBox.StandardButton.Yes
        ):
            self._mutate("無法略過複習", self.services.reviews.skip, item.schedule_id)

    def _edit_time(self) -> None:
        item = self._selected()
        if item is None:
            return
        dialog = DateTimeDialog(item.scheduled_at, parent=self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._mutate(
                "無法修改複習時間",
                self.services.reviews.reschedule,
                item.schedule_id,
                dialog.value(),
            )

    def _show_details(self) -> None:
        item = self._selected()
        if item is None:
            return
        try:
            details = self.services.tasks.details(item.task_id)
        except ServiceError as exc:
            self._show_error("無法開啟任務", exc)
            return
        TaskDetailsDialog(details, self).exec()

    def _manage_task(self) -> None:
        item = self._selected()
        if item is not None:
            self.manage_task_requested.emit(item.task_id)

    def _mutate(self, title: str, operation: Any, *arguments: object) -> None:
        try:
            operation(*arguments)
        except ServiceError as exc:
            self._show_error(title, exc)
            return
        self.refresh()
        self.data_changed.emit()


class TaskManagementPage(PageBase):
    def __init__(self, services: ApplicationServices, parent: QWidget | None = None) -> None:
        super().__init__(
            "任務管理",
            "新增、搜尋、篩選並管理所有學習任務。",
            (
                "所有新任務都從本頁右上方的『＋ 新增任務』建立。\n\n"
                "搜尋會同時查找任務名稱、說明、分類與標籤；也可以搭配狀態、分類和標籤篩選。\n\n"
                "勾選『只顯示這段日期內有複習的任務』後，只有在起訖日期內具有排程的任務會留下。\n\n"
                "選擇表格中的任務後，可從下方查看、編輯、複製、暫停、封存或刪除。"
            ),
            parent,
        )
        self.services = services
        self.items: list[TaskView] = []
        self._page_index = 0
        self._total_count = 0

        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("taskSearchEdit")
        self.search_edit.setPlaceholderText("搜尋名稱、說明、分類或標籤")
        self.search_edit.setClearButtonEnabled(True)
        top.addWidget(self.search_edit, 1)
        self.new_button = QPushButton("＋ 新增任務")
        self.new_button.setObjectName("primaryButton")
        top.addWidget(self.new_button)
        self.root_layout.addLayout(top)

        filters = QGridLayout()
        self.status_combo = QComboBox()
        self.status_combo.setObjectName("taskStatusFilter")
        self.category_combo = QComboBox()
        self.tag_combo = QComboBox()
        self.sort_combo = QComboBox()
        self.sort_combo.addItem("依建立時間", TaskSort.CREATED_AT.value)
        self.sort_combo.addItem("依任務名稱", TaskSort.NAME.value)
        self.sort_combo.addItem("依下次複習", TaskSort.NEXT_REVIEW.value)
        self.date_filter_check = QCheckBox("只顯示這段日期內有複習的任務")
        self.date_filter_check.setObjectName("taskScheduleDateFilter")
        self.date_filter_check.setToolTip(
            "勾選後，只保留至少一筆複習排程落在開始與結束日期之間的任務。"
        )
        self.from_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.to_date = QDateEdit(QDate.currentDate().addMonths(1))
        for editor in (self.from_date, self.to_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setEnabled(False)
        filters.addWidget(QLabel("狀態"), 0, 0)
        filters.addWidget(self.status_combo, 0, 1)
        filters.addWidget(QLabel("分類"), 0, 2)
        filters.addWidget(self.category_combo, 0, 3)
        filters.addWidget(QLabel("標籤"), 0, 4)
        filters.addWidget(self.tag_combo, 0, 5)
        filters.addWidget(QLabel("排序"), 1, 0)
        filters.addWidget(self.sort_combo, 1, 1)
        filters.addWidget(self.date_filter_check, 1, 2, 1, 4)
        filters.addWidget(QLabel("複習日期從"), 2, 2)
        filters.addWidget(self.from_date, 2, 3)
        filters.addWidget(QLabel("到"), 2, 4)
        filters.addWidget(self.to_date, 2, 5)
        self.root_layout.addLayout(filters)

        self.result_label = QLabel("0 個任務")
        self.result_label.setObjectName("resultCount")
        result_row = QHBoxLayout()
        result_row.addWidget(self.result_label)
        result_row.addStretch(1)
        self.previous_page_button = QPushButton("上一頁")
        self.page_label = QLabel()
        self.page_label.setObjectName("paginationLabel")
        self.next_page_button = QPushButton("下一頁")
        result_row.addWidget(self.previous_page_button)
        result_row.addWidget(self.page_label)
        result_row.addWidget(self.next_page_button)
        self.root_layout.addLayout(result_row)
        self.table = _table(("任務", "說明／筆記", "分類", "標籤", "狀態", "完成率", "下次複習"))
        self.table.setObjectName("taskTable")
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self.edit_selected())
        self.root_layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.detail_button = QPushButton("詳細資料")
        self.edit_button = QPushButton("編輯")
        self.copy_button = QPushButton("複製")
        self.pause_button = QPushButton("暫停")
        self.archive_button = QPushButton("封存")
        self.delete_button = QPushButton("刪除")
        self.delete_button.setObjectName("dangerButton")
        for button in (
            self.detail_button,
            self.edit_button,
            self.copy_button,
            self.pause_button,
            self.archive_button,
            self.delete_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)
        self.root_layout.addLayout(actions)

        self.new_button.clicked.connect(self.open_new)
        self.detail_button.clicked.connect(self.show_selected_details)
        self.edit_button.clicked.connect(self.edit_selected)
        self.copy_button.clicked.connect(self.copy_selected)
        self.pause_button.clicked.connect(self.toggle_pause)
        self.archive_button.clicked.connect(self.toggle_archive)
        self.delete_button.clicked.connect(self.delete_selected)
        self.search_edit.textChanged.connect(self._filters_changed)
        self.status_combo.currentIndexChanged.connect(self._filters_changed)
        self.category_combo.currentIndexChanged.connect(self._filters_changed)
        self.tag_combo.currentIndexChanged.connect(self._filters_changed)
        self.sort_combo.currentIndexChanged.connect(self._filters_changed)
        self.date_filter_check.toggled.connect(self._toggle_date_filter)
        self.from_date.dateChanged.connect(self._filters_changed)
        self.to_date.dateChanged.connect(self._filters_changed)
        self.previous_page_button.clicked.connect(lambda: self._change_page(-1))
        self.next_page_button.clicked.connect(lambda: self._change_page(1))
        self._populate_filters()
        self._update_actions()
        self.refresh()

    def _populate_filters(self) -> None:
        current_status = self.status_combo.currentData()
        current_category = self.category_combo.currentData()
        current_tag = self.tag_combo.currentData()
        blockers = (
            QSignalBlocker(self.status_combo),
            QSignalBlocker(self.category_combo),
            QSignalBlocker(self.tag_combo),
        )
        self.status_combo.clear()
        self.status_combo.addItem("全部（不含封存）", None)
        for status, label in STATUS_LABELS.items():
            self.status_combo.addItem(label, status.value)
        self.category_combo.clear()
        self.category_combo.addItem("全部分類", None)
        for item in self.services.tasks.categories():
            self.category_combo.addItem(item.name, item.id)
        self.tag_combo.clear()
        self.tag_combo.addItem("全部標籤", None)
        for item in self.services.tasks.tags():
            self.tag_combo.addItem(item.name, item.id)
        _restore_combo(self.status_combo, current_status)
        _restore_combo(self.category_combo, current_category)
        _restore_combo(self.tag_combo, current_tag)
        del blockers

    def _toggle_date_filter(self, enabled: bool) -> None:
        self.from_date.setEnabled(enabled)
        self.to_date.setEnabled(enabled)
        self._filters_changed()

    def _filters_changed(self) -> None:
        self._page_index = 0
        self.refresh()

    def refresh(self, *, focus_task_id: int | None = None) -> bool:
        try:
            self._populate_filters()
            page = self.services.tasks.query_page(
                self._current_query(),
                limit=TABLE_PAGE_SIZE,
                offset=self._page_index * TABLE_PAGE_SIZE,
                focus_task_id=focus_task_id,
            )
            self._page_index = page.offset // TABLE_PAGE_SIZE
            last_page = max(0, (page.total_count - 1) // TABLE_PAGE_SIZE)
            if self._page_index > last_page:
                self._page_index = last_page
                page = self.services.tasks.query_page(
                    self._current_query(),
                    limit=TABLE_PAGE_SIZE,
                    offset=self._page_index * TABLE_PAGE_SIZE,
                )
        except ServiceError as exc:
            self._show_error("無法載入任務", exc)
            return False
        self.items = list(page.items)
        self._total_count = page.total_count
        self.result_label.setText(f"{page.total_count} 個任務")
        self._update_pagination()
        self.table.setRowCount(len(self.items))
        for row, task in enumerate(self.items):
            values = (
                task.name,
                task.description or "—",
                task.category.name if task.category else "未分類",
                ", ".join(tag.name for tag in task.tags) or "—",
                STATUS_LABELS[task.status],
                f"{task.completion_rate:.0f}%",
                task.next_review_at.strftime(DATE_TIME_FORMAT) if task.next_review_at else "—",
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 1:
                    cell.setToolTip(task.description or "無說明／筆記")
                self.table.setItem(row, column, cell)
        self.table.resizeRowsToContents()
        self._update_actions()
        return True

    def _current_query(self) -> TaskQuery:
        status_value = self.status_combo.currentData()
        status = TaskStatus(status_value) if status_value else None
        use_dates = self.date_filter_check.isChecked()
        return TaskQuery(
            text=self.search_edit.text(),
            statuses=frozenset((status,)) if isinstance(status, TaskStatus) else frozenset(),
            category_id=self.category_combo.currentData(),
            tag_id=self.tag_combo.currentData(),
            scheduled_from=self.from_date.date().toPython() if use_dates else None,
            scheduled_to=self.to_date.date().toPython() if use_dates else None,
            include_archived=status is TaskStatus.ARCHIVED,
            sort_by=TaskSort(self.sort_combo.currentData() or TaskSort.CREATED_AT.value),
        )

    def _change_page(self, step: int) -> None:
        target = self._page_index + step
        page_count = (self._total_count + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE
        if not 0 <= target < page_count:
            return
        self._page_index = target
        self.refresh()

    def _update_pagination(self) -> None:
        page_count = (self._total_count + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE
        current = self._page_index + 1 if self._total_count else 0
        self.page_label.setText(f"第 {current} / {page_count} 頁")
        self.previous_page_button.setEnabled(self._page_index > 0)
        self.next_page_button.setEnabled(self._page_index + 1 < page_count)

    def _selected(self) -> TaskView | None:
        row = self.table.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def select_task(self, task_id: int) -> None:
        blockers = [QSignalBlocker(widget) for widget in (
            self.search_edit, self.status_combo, self.category_combo,
            self.tag_combo, self.date_filter_check,
        )]
        self.search_edit.clear()
        self.status_combo.setCurrentIndex(0)
        self.category_combo.setCurrentIndex(0)
        self.tag_combo.setCurrentIndex(0)
        self.date_filter_check.setChecked(False)
        self.from_date.setEnabled(False)
        self.to_date.setEnabled(False)
        del blockers
        self.table.setCurrentCell(-1, -1)
        if not self.refresh(focus_task_id=task_id):
            return
        for row, task in enumerate(self.items):
            if task.id == task_id:
                self.table.selectRow(row)
                self.table.scrollToItem(self.table.item(row, 0))
                break

    def _update_actions(self) -> None:
        task = self._selected()
        enabled = task is not None
        for button in (
            self.detail_button,
            self.edit_button,
            self.copy_button,
            self.pause_button,
            self.archive_button,
            self.delete_button,
        ):
            button.setEnabled(enabled)
        if task is not None:
            self.pause_button.setText("恢復" if task.is_paused else "暫停")
            self.archive_button.setText("還原封存" if task.is_archived else "封存")

    def open_new(self) -> None:
        dialog = TaskDialog(self.services.tasks, parent=self)
        if dialog.exec() == dialog.DialogCode.Accepted and dialog.result_draft is not None:
            self._operate("無法新增任務", self.services.tasks.create, dialog.result_draft)

    def edit_selected(self) -> None:
        task = self._selected()
        if task is None:
            return
        try:
            details = self.services.tasks.details(task.id)
        except ServiceError as exc:
            self._show_error("無法開啟任務", exc)
            return
        dialog = TaskDialog(self.services.tasks, details=details, parent=self)
        if dialog.exec() == dialog.DialogCode.Accepted and dialog.result_draft is not None:
            self._operate("無法編輯任務", self.services.tasks.update, task.id, dialog.result_draft)

    def copy_selected(self) -> None:
        task = self._selected()
        if task is None:
            return
        try:
            details = self.services.tasks.details(task.id)
        except ServiceError as exc:
            self._show_error("無法複製任務", exc)
            return
        dialog = TaskDialog(self.services.tasks, details=details, copy_mode=True, parent=self)
        if dialog.exec() == dialog.DialogCode.Accepted and dialog.result_draft is not None:
            self._operate("無法複製任務", self.services.tasks.create, dialog.result_draft)

    def show_selected_details(self) -> None:
        task = self._selected()
        if task is None:
            return
        try:
            details = self.services.tasks.details(task.id)
        except ServiceError as exc:
            self._show_error("無法開啟任務", exc)
            return
        TaskDetailsDialog(details, self).exec()

    def toggle_pause(self) -> None:
        task = self._selected()
        if task is None:
            return
        operation = self.services.tasks.resume if task.is_paused else self.services.tasks.pause
        self._operate("無法變更暫停狀態", operation, task.id)

    def toggle_archive(self) -> None:
        task = self._selected()
        if task is None:
            return
        operation = self.services.tasks.restore if task.is_archived else self.services.tasks.archive
        self._operate("無法變更封存狀態", operation, task.id)

    def delete_selected(self) -> None:
        task = self._selected()
        if task is None:
            return
        if (
            QMessageBox.question(
                self,
                "刪除任務",
                f"確定永久刪除「{task.name}」及其全部複習排程與歷史嗎？",
            )
            is QMessageBox.StandardButton.Yes
        ):
            self._operate("無法刪除任務", self.services.tasks.delete, task.id)

    def _operate(self, title: str, operation: Any, *arguments: object) -> None:
        try:
            operation(*arguments)
        except ServiceError as exc:
            self._show_error(title, exc)
            return
        self.refresh()
        self.data_changed.emit()


class DashboardPage(PageBase):
    navigate_requested = Signal(str)

    def __init__(self, services: ApplicationServices, parent: QWidget | None = None) -> None:
        super().__init__(
            "儀表板",
            "查看目前任務與複習進度摘要。",
            (
                "上方數字會排除已暫停與已封存的任務。\n\n"
                "未完成數包含待處理與已略過項目；總完成率只將已完成項目計入分子。\n\n"
                "使用快捷按鈕可前往今日任務、月曆或資料與備份。"
            ),
            parent,
        )
        self.services = services
        cards = QGridLayout()
        self.metrics: dict[str, QLabel] = {}
        definitions = (
            ("due", "今日待複習"),
            ("overdue", "逾期"),
            ("next", "未來 7 天"),
            ("tasks", "任務總數"),
            ("complete", "已完成複習"),
            ("incomplete", "未完成複習"),
            ("rate", "總完成率"),
        )
        for index, (key, title) in enumerate(definitions):
            card, value = _metric_card(title)
            self.metrics[key] = value
            cards.addWidget(card, index // 4, index % 4)
        self.root_layout.addLayout(cards)

        quick = QHBoxLayout()
        review = QPushButton("開始今日任務")
        review.setObjectName("primaryButton")
        overdue = QPushButton("查看逾期")
        calendar = QPushButton("開啟月曆")
        backup = QPushButton("資料與備份")
        review.clicked.connect(lambda: self.navigate_requested.emit("reviews"))
        overdue.clicked.connect(lambda: self.navigate_requested.emit("reviews"))
        calendar.clicked.connect(lambda: self.navigate_requested.emit("calendar"))
        backup.clicked.connect(lambda: self.navigate_requested.emit("backup"))
        for button in (review, overdue, calendar, backup):
            quick.addWidget(button)
        quick.addStretch(1)
        self.root_layout.addLayout(quick)

        upcoming_title = QLabel("今日與逾期項目")
        upcoming_title.setObjectName("sectionTitle")
        self.root_layout.addWidget(upcoming_title)
        self.upcoming_table = _table(("類型", "任務", "預定時間", "完成率"))
        self.upcoming_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.root_layout.addWidget(self.upcoming_table, 1)
        self.refresh()

    def refresh(self) -> None:
        try:
            summary = self.services.tasks.dashboard_summary()
            overdue = self.services.reviews.overdue(limit=10)
            remaining = 10 - len(overdue)
            today = self.services.reviews.due_today(limit=remaining) if remaining else ()
        except ServiceError as exc:
            self._show_error("無法載入儀表板", exc)
            return
        self._set_summary(summary)
        items = [*overdue, *today][:10]
        self.upcoming_table.setRowCount(len(items))
        today_date = self.services.reviews.now_provider().date()
        for row, item in enumerate(items):
            values = (
                "逾期" if item.scheduled_at.date() < today_date else "今日",
                item.task_name,
                item.scheduled_at.strftime(DATE_TIME_FORMAT),
                f"{item.completion_rate:.0f}%",
            )
            for column, value in enumerate(values):
                self.upcoming_table.setItem(row, column, QTableWidgetItem(value))

    def _set_summary(self, summary: DashboardSummary) -> None:
        self.metrics["due"].setText(str(summary.due_today_count))
        self.metrics["overdue"].setText(str(summary.overdue_count))
        self.metrics["next"].setText(str(summary.next_seven_days_count))
        self.metrics["tasks"].setText(str(summary.task_total))
        self.metrics["complete"].setText(str(summary.completed_review_count))
        self.metrics["incomplete"].setText(str(summary.incomplete_review_count))
        self.metrics["rate"].setText(f"{summary.completion_rate:.0f}%")


class ReviewCalendarWidget(QCalendarWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summaries: dict[date, CalendarDaySummary] = {}
        self.hovered_date: QDate | None = None
        self._hover_alpha = 0
        self._hover_motion = QVariantAnimation(self)
        self._hover_motion.setDuration(150)
        self._hover_motion.valueChanged.connect(self._animate_hover)
        self.setGridVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self._calendar_view = self.findChild(QTableView, "qt_calendar_calendarview")
        self._calendar_viewport: QWidget | None = None
        if self._calendar_view is not None:
            self._calendar_viewport = self._calendar_view.viewport()
            self._calendar_viewport.setMouseTracking(True)
            self._calendar_viewport.setCursor(Qt.CursorShape.PointingHandCursor)
            self._calendar_viewport.installEventFilter(self)

    def set_summaries(self, summaries: dict[date, CalendarDaySummary]) -> None:
        self.summaries = summaries
        self.updateCells()

    @staticmethod
    def workload_colors(count: int) -> tuple[QColor, QColor]:
        """Fixed bands keep workload colors comparable when changing months."""
        if count <= 2:
            return QColor("#eee7fa"), QColor("#493568")
        if count <= 5:
            return QColor("#c3addf"), QColor("#302042")
        if count <= 9:
            return QColor("#8060aa"), QColor("#ffffff")
        return QColor("#493064"), QColor("#ffffff")

    def paintCell(self, painter: QPainter, rect: QRect, value: QDate) -> None:
        super().paintCell(painter, rect, value)
        if value == self.hovered_date:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            hover_fill = QColor("#d8c9f5")
            hover_fill.setAlpha(self._hover_alpha)
            painter.setBrush(hover_fill)
            painter.setPen(QPen(QColor("#9b82cd"), 2))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 6, 6)
            painter.restore()
        summary = self.summaries.get(value.toPython())
        if summary is None or summary.pending_count == 0:
            return
        painter.save()
        color, text_color = self.workload_colors(summary.pending_count)
        painter.setBrush(color)
        painter.setPen(QPen(QColor("#bd6670") if summary.overdue_count else color,
                            2 if summary.overdue_count else 1))
        size = min(20, rect.height() - 4)
        text_width = painter.fontMetrics().horizontalAdvance(str(summary.pending_count))
        width = min(rect.width() - 6, max(size, text_width + 8))
        badge = QRect(rect.right() - width - 3, rect.bottom() - size - 3, width, size)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawRoundedRect(badge, size / 2, size / 2)
        painter.setPen(text_color)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(summary.pending_count))
        painter.restore()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._calendar_view is not None and watched is self._calendar_viewport:
            if event.type() is QEvent.Type.Wheel:
                event.accept()
                return True
            if event.type() is QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
                index = self._calendar_view.indexAt(event.position().toPoint())
                self._set_hovered_date(self._date_for_index(index))
            elif event.type() is QEvent.Type.Leave:
                self._set_hovered_date(None)
        return super().eventFilter(watched, event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()

    def _date_for_index(self, index: QModelIndex) -> QDate | None:
        if not index.isValid() or index.row() < 1:
            return None
        first_of_month = QDate(self.yearShown(), self.monthShown(), 1)
        leading_days = (first_of_month.dayOfWeek() - self.firstDayOfWeek().value + 7) % 7
        date_column = index.column()
        if index.model().columnCount() == 8:
            if date_column < 1:
                return None
            date_column -= 1
        return first_of_month.addDays(-leading_days + ((index.row() - 1) * 7) + date_column)

    def _set_hovered_date(self, value: QDate | None) -> None:
        if value == self.hovered_date:
            return
        previous = self.hovered_date
        self._hover_motion.stop()
        self.hovered_date = value
        if previous is not None:
            self.updateCell(previous)
        if value is not None:
            self._hover_motion.setStartValue(20)
            self._hover_motion.setEndValue(120)
            self._hover_motion.start()
            self.updateCell(value)

    def _animate_hover(self, alpha: int) -> None:
        self._hover_alpha = alpha
        if self.hovered_date is not None:
            self.updateCell(self.hovered_date)


class CalendarPage(PageBase):
    def __init__(self, services: ApplicationServices, parent: QWidget | None = None) -> None:
        super().__init__(
            "月曆",
            "查看每日待複習數量，並直接處理選定日期的項目。",
            (
                "日期右下角的圓形數字代表當天待處理的複習數量，紅色代表逾期。\n\n"
                "移動游標到日期格會顯示外框；切換月份請使用月曆上方的左右箭頭，滾輪不會切換月份。\n\n"
                "點擊日期後，右側會顯示選定日期與該日清單；沒有項目時也會顯示提示。\n\n"
                "選擇右側項目後，可完成、推到明天、編輯時間或查看任務詳細資料。"
            ),
            parent,
        )
        self.services = services
        self.items: list[ReviewItem] = []
        self._day_page_index = 0
        self._day_total_count = 0
        toolbar = QHBoxLayout()
        today_button = QPushButton("回到今天")
        today_button.clicked.connect(self._go_today)
        self.month_label = QLabel()
        self.month_label.setObjectName("sectionTitle")
        toolbar.addWidget(today_button)
        toolbar.addWidget(self.month_label)
        toolbar.addStretch(1)
        self.root_layout.addLayout(toolbar)

        content = QHBoxLayout()
        self.calendar = ReviewCalendarWidget()
        legend = QLabel("待複習數：1–2 淺紫 · 3–5 中紫 · 6–9 深紫 · 10+ 最深｜紅框＝有逾期")
        legend.setWordWrap(True)
        legend.setObjectName("calendarWorkloadLegend")
        self.root_layout.addWidget(legend)
        self.calendar.setObjectName("reviewCalendar")
        today = self.services.reviews.now_provider().date()
        self.calendar.setSelectedDate(QDate(today.year, today.month, today.day))
        content.addWidget(self.calendar, 3)
        right = QVBoxLayout()
        self.day_label = QLabel()
        self.day_label.setObjectName("sectionTitle")
        right.addWidget(self.day_label)
        self.selection_feedback = QLabel()
        self.selection_feedback.setObjectName("calendarSelectionFeedback")
        self.selection_feedback.setWordWrap(True)
        right.addWidget(self.selection_feedback)
        self.empty_label = QLabel(
            "這一天沒有待處理的複習。請點選有數字徽章的日期，或切換月份查看其他安排。"
        )
        self.empty_label.setObjectName("emptyState")
        self.empty_label.setWordWrap(True)
        right.addWidget(self.empty_label)
        self.table = _table(("任務", "時間", "次數", "完成率"))
        self.table.setObjectName("calendarDayTable")
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_actions)
        right.addWidget(self.table, 1)
        pagination = QHBoxLayout()
        self.previous_page_button = QPushButton("上一頁")
        self.day_page_label = QLabel()
        self.day_page_label.setObjectName("paginationLabel")
        self.next_page_button = QPushButton("下一頁")
        pagination.addStretch(1)
        pagination.addWidget(self.previous_page_button)
        pagination.addWidget(self.day_page_label)
        pagination.addWidget(self.next_page_button)
        right.addLayout(pagination)
        action_grid = QGridLayout()
        self.complete_button = QPushButton("完成")
        self.postpone_button = QPushButton("推到明天")
        self.edit_button = QPushButton("編輯時間")
        self.detail_button = QPushButton("詳細資料")
        for index, button in enumerate(
            (
                self.complete_button,
                self.postpone_button,
                self.edit_button,
                self.detail_button,
            )
        ):
            action_grid.addWidget(button, index // 2, index % 2)
        right.addLayout(action_grid)
        content.addLayout(right, 2)
        self.root_layout.addLayout(content, 1)

        self.calendar.currentPageChanged.connect(self._refresh_month)
        self.calendar.selectionChanged.connect(self._selected_day_changed)
        self.calendar.clicked.connect(self._on_date_clicked)
        self.complete_button.clicked.connect(self._complete)
        self.postpone_button.clicked.connect(self._postpone)
        self.edit_button.clicked.connect(self._edit_time)
        self.detail_button.clicked.connect(self._show_details)
        self.previous_page_button.clicked.connect(lambda: self._change_day_page(-1))
        self.next_page_button.clicked.connect(lambda: self._change_day_page(1))
        self._update_actions()
        self.refresh()

    def refresh(self) -> None:
        self._refresh_month()
        self._refresh_day()

    def _refresh_month(self) -> None:
        year = self.calendar.yearShown()
        month = self.calendar.monthShown()
        previous_month = getattr(self, "_display_month", (year, month))
        self._display_month = (year, month)
        last_day = calendar_module.monthrange(year, month)[1]
        try:
            summaries = self.services.tasks.calendar(
                date(year, month, 1), date(year, month, last_day)
            )
        except ServiceError as exc:
            self._show_error("無法載入月曆", exc)
            return
        self.month_label.setText(f"{year} 年 {month} 月")
        self.calendar.set_summaries({item.day: item for item in summaries})
        if previous_month != (year, month):
            from task_assignment.ui.learning_visuals import motion_mode, reveal

            reveal(self.calendar, motion_mode(self), 1 if (year, month) > previous_month else -1)

    def _refresh_day(self) -> None:
        selected = self.calendar.selectedDate().toPython()
        try:
            page = self.services.reviews.day_page(
                selected,
                limit=TABLE_PAGE_SIZE,
                offset=self._day_page_index * TABLE_PAGE_SIZE,
            )
            last_page = max(0, (page.total_count - 1) // TABLE_PAGE_SIZE)
            if self._day_page_index > last_page:
                self._day_page_index = last_page
                page = self.services.reviews.day_page(
                    selected,
                    limit=TABLE_PAGE_SIZE,
                    offset=self._day_page_index * TABLE_PAGE_SIZE,
                )
        except ServiceError as exc:
            self._show_error("無法載入日期項目", exc)
            return
        self.items = list(page.items)
        self._day_total_count = page.total_count
        self.day_label.setText(f"{selected:%Y 年 %m 月 %d 日} · {page.total_count} 筆待複習")
        self.selection_feedback.setText(f"已選擇 {selected:%Y 年 %m 月 %d 日}。右側清單已更新。")
        self.empty_label.setVisible(not page.total_count)
        self._update_day_pagination()
        self.table.setRowCount(len(self.items))
        for row, item in enumerate(self.items):
            values = (
                item.task_name,
                item.scheduled_at.strftime("%H:%M"),
                f"第 {item.sequence} 次",
                f"{item.completion_rate:.0f}%",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self._update_actions()

    def _selected_day_changed(self) -> None:
        self._day_page_index = 0
        self._refresh_day()
        # Date selection is direct manipulation: show the new list immediately.

    def _change_day_page(self, step: int) -> None:
        target = self._day_page_index + step
        page_count = (self._day_total_count + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE
        if not 0 <= target < page_count:
            return
        self._day_page_index = target
        self._refresh_day()

    def _update_day_pagination(self) -> None:
        page_count = (self._day_total_count + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE
        current = self._day_page_index + 1 if self._day_total_count else 0
        self.day_page_label.setText(f"第 {current} / {page_count} 頁")
        self.previous_page_button.setEnabled(self._day_page_index > 0)
        self.next_page_button.setEnabled(self._day_page_index + 1 < page_count)

    def _on_date_clicked(self, selected: QDate) -> None:
        self.selection_feedback.setText(
            f"已選擇 {selected.toPython():%Y 年 %m 月 %d 日}。右側清單已更新。"
        )

    def _go_today(self) -> None:
        self.calendar.setSelectedDate(QDate.currentDate())
        self.calendar.showToday()

    def _selected(self) -> ReviewItem | None:
        row = self.table.currentRow()
        return self.items[row] if 0 <= row < len(self.items) else None

    def _update_actions(self) -> None:
        enabled = self._selected() is not None
        for button in (
            self.complete_button,
            self.postpone_button,
            self.edit_button,
            self.detail_button,
        ):
            button.setEnabled(enabled)

    def _complete(self) -> None:
        item = self._selected()
        if item is not None:
            self._mutate("無法完成複習", self.services.reviews.complete, item.schedule_id)

    def _postpone(self) -> None:
        item = self._selected()
        if item is None:
            return
        if (
            QMessageBox.question(
                self,
                "推到明天",
                "目前項目與同任務後續待處理排程都會順延一天，確定繼續嗎？",
            )
            is QMessageBox.StandardButton.Yes
        ):
            self._mutate("無法推延複習", self.services.reviews.postpone, item.schedule_id)

    def _edit_time(self) -> None:
        item = self._selected()
        if item is None:
            return
        dialog = DateTimeDialog(item.scheduled_at, parent=self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._mutate(
                "無法修改複習時間",
                self.services.reviews.reschedule,
                item.schedule_id,
                dialog.value(),
            )

    def _show_details(self) -> None:
        item = self._selected()
        if item is None:
            return
        try:
            details = self.services.tasks.details(item.task_id)
        except ServiceError as exc:
            self._show_error("無法開啟任務", exc)
            return
        TaskDetailsDialog(details, self).exec()

    def _mutate(self, title: str, operation: Any, *arguments: object) -> None:
        try:
            operation(*arguments)
        except ServiceError as exc:
            self._show_error(title, exc)
            return
        self.refresh()
        self.data_changed.emit()


class BackupPage(PageBase):
    restart_required = Signal(object)

    def __init__(self, services: ApplicationServices, parent: QWidget | None = None) -> None:
        super().__init__(
            "資料與備份",
            "建立可驗證的完整 ZIP，或用既有 ZIP 完整取代目前資料。",
            "「建立備份」會保存資料庫、背景與貼圖。匯入前會先驗證 ZIP，"
            "並自動建立目前資料的還原點；匯入成功後必須重新啟動程式。",
            parent,
        )
        self.services = services
        self._thread: QThread | None = None
        self._worker: _OperationWorker | None = None
        self._operation_kind = ""
        self._operation_active = False
        self._queued_operation: tuple[str, Callable[[], object]] | None = None
        self._gmail_draft_dialog: GmailDraftDialog | None = None

        warning = QFrame()
        warning.setObjectName("backupWarning")
        warning_layout = QVBoxLayout(warning)
        warning_title = QLabel("未加密備份提醒")
        warning_title.setObjectName("backupWarningTitle")
        warning_text = QLabel(
            "備份 ZIP 未加密，取得檔案的人可以查看其中的任務內容。請將檔案保存在可信任的位置。"
        )
        warning_text.setWordWrap(True)
        warning_layout.addWidget(warning_title)
        warning_layout.addWidget(warning_text)
        self.root_layout.addWidget(warning)

        actions = QGroupBox("本機 ZIP")
        actions_layout = QVBoxLayout(actions)
        action_text = QLabel(
            "備份包含 SQLite 資料庫及已上傳的背景、貼圖；"
            "不包含 OAuth 認證、既有備份 ZIP 或文字日誌。"
        )
        action_text.setWordWrap(True)
        actions_layout.addWidget(action_text)
        button_row = QHBoxLayout()
        self.create_button = QPushButton("建立備份")
        self.create_button.setObjectName("primaryButton")
        self.create_button.setAccessibleName("建立完整 ZIP 備份")
        self.import_button = QPushButton("匯入 ZIP（完整取代）")
        self.import_button.setAccessibleName("匯入 ZIP 並完整取代目前資料")
        self.gmail_button = QPushButton("建立並寄送 Gmail 備份")
        self.gmail_button.setAccessibleName("建立 ZIP 並以 Gmail 手動寄送")
        button_row.addWidget(self.create_button)
        button_row.addWidget(self.gmail_button)
        button_row.addWidget(self.import_button)
        button_row.addStretch(1)
        actions_layout.addLayout(button_row)
        self.gmail_status_label = QLabel()
        self.gmail_status_label.setObjectName("gmailStatus")
        self.gmail_status_label.setWordWrap(True)
        actions_layout.addWidget(self.gmail_status_label)
        self.root_layout.addWidget(actions)

        result_panel = QFrame()
        result_panel.setObjectName("backupResultPanel")
        result_layout = QVBoxLayout(result_panel)
        result_title = QLabel("最近一次操作")
        result_title.setObjectName("backupResultTitle")
        self.status_label = QLabel("尚未進行備份或匯入。")
        self.status_label.setObjectName("backupStatus")
        self.status_label.setWordWrap(True)
        self.detail_label = QLabel("備份檔名格式：task-assignment-backup-YYYYMMDD-HHMMSS.zip")
        self.detail_label.setObjectName("backupDetail")
        self.detail_label.setWordWrap(True)
        result_layout.addWidget(result_title)
        result_layout.addWidget(self.status_label)
        result_layout.addWidget(self.detail_label)
        self.root_layout.addWidget(result_panel)
        self.root_layout.addStretch(1)

        self.create_button.clicked.connect(self._create_backup)
        self.import_button.clicked.connect(self._choose_import)
        self.gmail_button.clicked.connect(self._prepare_gmail)
        self.refresh()

    @property
    def operation_in_progress(self) -> bool:
        return self._operation_active

    def _create_backup(self) -> None:
        self._start_operation("backup", self.services.backups.create_backup)

    def refresh(self) -> None:
        try:
            status = self.services.gmail.status()
        except ServiceError as exc:
            self.gmail_status_label.setText(str(exc))
            self.gmail_button.setEnabled(False)
            return
        if not status.configured:
            self.gmail_status_label.setText(
                "此版本尚未開放 Gmail；請先使用本機 ZIP 備份。開放後只需登入 Google 帳號。"
            )
            self.gmail_button.setEnabled(False)
            self.gmail_button.setToolTip("請等待支援 Gmail 的版本，無需自行準備設定檔。")
        elif status.linked:
            self.gmail_status_label.setText(f"Gmail：已連結 {status.account_email}")
            self.gmail_button.setEnabled(self._thread is None)
            self.gmail_button.setToolTip("")
        else:
            self.gmail_status_label.setText("Gmail：尚未連結，第一次寄送時會開啟系統瀏覽器授權。")
            self.gmail_button.setEnabled(self._thread is None)
            self.gmail_button.setToolTip("")

    def _prepare_gmail(self) -> None:
        self._start_operation("gmail_prepare", self.services.gmail.prepare_email)

    def _choose_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "選擇 Task Assignment 備份",
            "",
            "Task Assignment 備份 (*.zip)",
        )
        if not path:
            return
        answer = QMessageBox.warning(
            self,
            "完整取代目前資料",
            "匯入會以 ZIP 中的資料庫、背景與貼圖完整取代目前內容。\n\n"
            "系統會先建立匯入前還原點；成功後必須重新啟動程式。確定繼續嗎？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._start_operation("import", lambda: self.services.backups.import_backup(path))

    def _start_operation(self, kind: str, operation: Callable[[], object]) -> None:
        if self._thread is not None:
            return
        if not self._begin_operation(kind):
            return
        self._operation_kind = kind
        self._operation_active = True
        self._set_action_buttons(False)
        messages = {
            "backup": "正在建立並驗證備份…",
            "import": "正在驗證、建立還原點並匯入…",
            "gmail_prepare": "正在建立 Gmail 郵件所需的本機備份…",
            "gmail_send": "正在完成 Gmail 授權並寄送…",
        }
        self.status_label.setText(messages.get(kind, "正在處理…"))
        self.detail_label.setText("操作期間請勿關閉程式。")

        thread = QThread(self)
        worker = _OperationWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._operation_succeeded)
        worker.failed.connect(self._operation_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot(object)
    def _operation_succeeded(self, result: object) -> None:
        if isinstance(result, BackupResult):
            self.status_label.setText(f"備份建立成功：{result.path.name}")
            self.detail_label.setText(
                f"位置：{result.path}\n"
                f"ZIP 大小：{_format_bytes(result.size)}｜內容檔案：{result.file_count} 個｜"
                f"SHA-256：{result.archive_sha256}"
            )
            return
        if isinstance(result, ImportResult):
            self.restart_required.emit(result)
            self.status_label.setText("匯入完成。為避免操作已取代的資料，現在必須重新啟動程式。")
            detail = f"匯入前還原點：{result.restore_point}"
            if result.warning:
                detail += f"\n注意：{result.warning}"
            self.detail_label.setText(detail)
            QMessageBox.information(
                self,
                "匯入完成",
                "資料庫與素材已完整取代，匯入前還原點已保留。\n\n"
                "請關閉 Task Assignment 後重新開啟。",
            )
            return
        if isinstance(result, GmailDraft):
            dialog = GmailDraftDialog(result, self)
            self._gmail_draft_dialog = dialog
            dialog.setModal(True)
            dialog.accepted.connect(lambda: self._accept_gmail_draft(result, dialog))
            dialog.rejected.connect(lambda: self._reject_gmail_draft(result, dialog))
            dialog.open()
            return
        if isinstance(result, GmailSendResult):
            self.status_label.setText(f"Gmail 備份已寄送至 {result.recipient}。")
            detail = f"本機 ZIP：{result.backup_path}\nGmail message ID：{result.message_id}"
            if result.warning:
                detail += f"\n注意：{result.warning}"
            self.detail_label.setText(detail)
            QMessageBox.information(
                self,
                "Gmail 寄送完成",
                "備份郵件已寄送，ZIP 仍保留在本機備份目錄。",
            )

    def _accept_gmail_draft(
        self,
        draft: GmailDraft,
        dialog: GmailDraftDialog,
    ) -> None:
        if dialog is not self._gmail_draft_dialog:
            return
        self._gmail_draft_dialog = None
        if dialog.save_default_check.isChecked():
            try:
                self.services.gmail.set_default_recipient(dialog.recipient())
            except ServiceError as exc:
                self._show_error("無法保存預設收件人", exc)
                self.status_label.setText("本機 ZIP 已保留，尚未寄送。")
                self.detail_label.setText(f"保留位置：{draft.backup_path}")
                self._finish_draft_without_send()
                dialog.deleteLater()
                return
        recipient = dialog.recipient()
        subject = dialog.subject()
        body = dialog.body()

        def operation() -> object:
            return self.services.gmail.send_prepared(
                draft,
                recipient=recipient,
                subject=subject,
                body=body,
            )

        if self._thread is None:
            QTimer.singleShot(0, lambda: self._start_operation("gmail_send", operation))
        else:
            self._queued_operation = ("gmail_send", operation)
        dialog.deleteLater()

    def _reject_gmail_draft(
        self,
        draft: GmailDraft,
        dialog: GmailDraftDialog,
    ) -> None:
        if dialog is not self._gmail_draft_dialog:
            return
        self._gmail_draft_dialog = None
        self.status_label.setText("已建立本機 ZIP；使用者取消 Gmail 寄送。")
        self.detail_label.setText(f"保留位置：{draft.backup_path}")
        self._finish_draft_without_send()
        dialog.deleteLater()

    def _finish_draft_without_send(self) -> None:
        if self._thread is None:
            self._end_operation()
            self._set_action_buttons(True)
            self.refresh()
            self._operation_active = False

    @Slot(object)
    def _operation_failed(self, error: object) -> None:
        if isinstance(error, ServiceError):
            message = str(error)
        else:
            message = "發生未預期錯誤，目前資料未完成變更。"
        self.status_label.setText("操作失敗。")
        if self._operation_kind == "gmail_send":
            message += "\n已建立的 ZIP 仍保留在本機備份目錄。"
        self.detail_label.setText(message)
        titles = {
            "backup": "備份失敗",
            "import": "匯入失敗",
            "gmail_prepare": "無法準備 Gmail 備份",
            "gmail_send": "Gmail 寄送失敗",
        }
        QMessageBox.warning(
            self,
            titles.get(self._operation_kind, "操作失敗"),
            message,
        )

    @Slot()
    def _thread_finished(self) -> None:
        self._worker = None
        self._thread = None
        queued = self._queued_operation
        self._queued_operation = None
        if queued is not None:
            kind, operation = queued
            QTimer.singleShot(0, lambda: self._start_operation(kind, operation))
            return
        if self._gmail_draft_dialog is not None:
            return
        self._end_operation()
        if getattr(self.window(), "_restart_pending", False):
            self._operation_active = False
            return
        self._set_action_buttons(True)
        self.refresh()
        self._operation_active = False

    def _set_action_buttons(self, enabled: bool) -> None:
        self.create_button.setEnabled(enabled)
        self.import_button.setEnabled(enabled)
        self.gmail_button.setEnabled(enabled)


PERSONALIZATION_PAGES = (
    ("reviews", "今日任務"),
    ("tasks", "任務管理"),
    ("calendar", "月曆"),
    ("dashboard", "儀表板"),
    ("backup", "資料與備份"),
    ("settings", "個人化設定"),
)


class PersonalizationPage(PageBase):
    appearance_changed = Signal()

    def __init__(self, services: ApplicationServices, parent: QWidget | None = None) -> None:
        super().__init__(
            "個人化設定",
            "上傳並套用自己的背景與貼圖。",
            (
                "背景與貼圖支援 PNG、JPG、JPEG、WebP，單一檔案不可超過 20 MB。\n\n"
                "背景可套用到全部頁面，或只套用到指定頁面；選擇『不使用自訂背景』可恢復預設。\n\n"
                "貼圖固定顯示在左側導覽下方，不提供位置或尺寸調整。刪除使用中的素材時會自動恢復預設或停用。\n\n"
                "Gmail 區可保存預設收件人、連結、重新授權或解除連結；程式不會要求 Gmail 密碼。"
            ),
            parent,
        )
        self.services = services
        self._gmail_thread: QThread | None = None
        self._gmail_worker: _OperationWorker | None = None

        self.scroll = QScrollArea()
        self.scroll.setObjectName("personalizationScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("personalizationContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(14)

        motion_group = QGroupBox("介面動畫")
        motion_layout = QFormLayout(motion_group)
        self.motion_combo = QComboBox()
        self.motion_combo.addItem("完整 · 流暢漸變", "full")
        self.motion_combo.addItem("減少 · 輕量回饋", "reduced")
        self.motion_combo.addItem("關閉", "off")
        mode = self.services.settings.get("interface_motion", "full")
        self.motion_combo.setCurrentIndex(max(0, self.motion_combo.findData(mode)))
        self.motion_combo.currentIndexChanged.connect(self._save_motion)
        motion_layout.addRow("動畫效果", self.motion_combo)
        self.motion_preview_button = QPushButton("播放完成效果預覽")
        self.motion_preview_button.clicked.connect(self._preview_motion)
        motion_layout.addRow("示意預覽", self.motion_preview_button)
        content_layout.addWidget(motion_group)

        scale_group = QGroupBox("介面縮放")
        scale_layout = QFormLayout(scale_group)
        self.scale_combo = QComboBox()
        self.scale_combo.addItem("自動（依視窗大小連續調整）", "auto")
        current_scale_mode = self.services.settings.get("interface_scale_mode", "auto")
        scale_index = self.scale_combo.findData(current_scale_mode)
        self.scale_combo.setCurrentIndex(scale_index if scale_index >= 0 else 0)
        self.scale_combo.currentIndexChanged.connect(self._save_scale)
        scale_layout.addRow("物件與文字大小", self.scale_combo)
        scale_hint = QLabel(
            "視窗放大時會連續放大文字、按鈕、卡片與輸入區塊；視窗縮小時會自動收斂，"
            "不會鎖定 110% 或 125%。"
        )
        scale_hint.setWordWrap(True)
        scale_layout.addRow("使用方式", scale_hint)
        content_layout.addWidget(scale_group)

        version_group = QGroupBox("版本資訊")
        version_layout = QFormLayout(version_group)
        version_layout.addRow("目前版本", QLabel(APP_VERSION))
        version_layout.addRow("版本號碼", QLabel(VERSION_NUMBER))
        version_layout.addRow("本次更新時間", QLabel(f"{RELEASE_UPDATED_AT}（台北時間）"))
        content_layout.addWidget(version_group)

        background_group = QGroupBox("背景")
        background_layout = QGridLayout(background_group)
        self.background_preview = QLabel("目前未選擇背景")
        self.background_preview.setObjectName("assetPreview")
        self.background_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.background_preview.setFixedHeight(120)
        self.background_preview.setMinimumWidth(0)
        self.background_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        background_layout.addWidget(self.background_preview, 0, 0, 1, 4)
        background_layout.addWidget(QLabel("已上傳背景"), 1, 0)
        self.background_combo = QComboBox()
        self.background_combo.setObjectName("backgroundAssetCombo")
        background_layout.addWidget(self.background_combo, 1, 1, 1, 3)
        background_layout.addWidget(QLabel("套用方式"), 2, 0)
        self.background_mode_combo = QComboBox()
        self.background_mode_combo.addItem("全部頁面", "global")
        self.background_mode_combo.addItem("指定頁面", "page")
        background_layout.addWidget(self.background_mode_combo, 2, 1)
        self.background_page_label = QLabel("套用頁面")
        background_layout.addWidget(self.background_page_label, 2, 2)
        self.background_page_combo = QComboBox()
        for page_id, label in PERSONALIZATION_PAGES:
            self.background_page_combo.addItem(label, page_id)
        background_layout.addWidget(self.background_page_combo, 2, 3)
        self.upload_background_button = QPushButton("上傳背景")
        self.apply_background_button = QPushButton("套用背景")
        self.apply_background_button.setObjectName("primaryButton")
        self.delete_background_button = QPushButton("刪除選取背景")
        self.delete_background_button.setObjectName("dangerButton")
        background_layout.addWidget(self.upload_background_button, 3, 0)
        background_layout.addWidget(self.apply_background_button, 3, 1)
        background_layout.addWidget(self.delete_background_button, 3, 2, 1, 2)
        content_layout.addWidget(background_group)

        sticker_group = QGroupBox("貼圖")
        sticker_layout = QGridLayout(sticker_group)
        self.sticker_preview = QLabel("目前未選擇貼圖")
        self.sticker_preview.setObjectName("stickerPreview")
        self.sticker_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sticker_preview.setFixedSize(112, 112)
        sticker_layout.addWidget(self.sticker_preview, 0, 0, 3, 1)
        sticker_layout.addWidget(QLabel("已上傳貼圖"), 0, 1)
        self.sticker_combo = QComboBox()
        self.sticker_combo.setObjectName("stickerAssetCombo")
        sticker_layout.addWidget(self.sticker_combo, 0, 2, 1, 2)
        self.sticker_enabled_check = QCheckBox("套用並顯示貼圖（主視窗左側導覽列底部）")
        sticker_layout.addWidget(self.sticker_enabled_check, 1, 1, 1, 3)
        sticker_location = QLabel(
            "貼圖不會覆蓋任務內容；套用後會出現在主視窗左側深色導覽列的最下方。"
        )
        sticker_location.setWordWrap(True)
        sticker_layout.addWidget(sticker_location, 3, 1, 1, 3)
        self.upload_sticker_button = QPushButton("上傳貼圖")
        self.apply_sticker_button = QPushButton("套用貼圖設定")
        self.apply_sticker_button.setObjectName("primaryButton")
        self.delete_sticker_button = QPushButton("刪除選取貼圖")
        self.delete_sticker_button.setObjectName("dangerButton")
        sticker_layout.addWidget(self.upload_sticker_button, 2, 1)
        sticker_layout.addWidget(self.apply_sticker_button, 2, 2)
        sticker_layout.addWidget(self.delete_sticker_button, 2, 3)
        content_layout.addWidget(sticker_group)

        gmail_group = QGroupBox("Gmail 帳號與預設收件人")
        gmail_layout = QGridLayout(gmail_group)
        self.gmail_account_status = QLabel()
        self.gmail_account_status.setObjectName("gmailAccountStatus")
        self.gmail_account_status.setWordWrap(True)
        gmail_layout.addWidget(self.gmail_account_status, 0, 0, 1, 4)
        gmail_layout.addWidget(QLabel("預設收件人"), 1, 0)
        self.gmail_recipient_edit = QLineEdit()
        self.gmail_recipient_edit.setPlaceholderText("name@example.com（可留空）")
        gmail_layout.addWidget(self.gmail_recipient_edit, 1, 1, 1, 2)
        self.gmail_save_recipient_button = QPushButton("保存收件人")
        gmail_layout.addWidget(self.gmail_save_recipient_button, 1, 3)
        self.gmail_connect_button = QPushButton("連結 Gmail")
        self.gmail_connect_button.setObjectName("primaryButton")
        self.gmail_reauthorize_button = QPushButton("重新授權")
        self.gmail_disconnect_button = QPushButton("解除連結")
        self.gmail_disconnect_button.setObjectName("dangerButton")
        gmail_layout.addWidget(self.gmail_connect_button, 2, 0)
        gmail_layout.addWidget(self.gmail_reauthorize_button, 2, 1)
        gmail_layout.addWidget(self.gmail_disconnect_button, 2, 2)
        self.gmail_help_label = QLabel(
            "使用方式：連結 Gmail → 在 Google 官方頁面登入並同意授權 → 回到程式。\n"
            "連結不會寄出郵件。寄送請到「資料與備份」，確認收件人與內容後才會寄出。\n"
            "不需要下載或修改設定檔，也不用在此輸入 Google 密碼；僅要求寄信及辨識帳號的權限，"
            "不讀取收件匣。"
        )
        self.gmail_help_label.setWordWrap(True)
        gmail_layout.addWidget(self.gmail_help_label, 3, 0, 1, 4)
        gmail_layout.setColumnStretch(3, 1)
        content_layout.addWidget(gmail_group)

        self.status_label = QLabel("可上傳 PNG、JPG、JPEG、WebP；每張最多 20 MB。")
        self.status_label.setObjectName("personalizationStatus")
        self.status_label.setWordWrap(True)
        content_layout.addWidget(self.status_label)
        content_layout.addStretch(1)
        self.scroll.setWidget(content)
        self.root_layout.addWidget(self.scroll, 1)

        self.upload_background_button.clicked.connect(lambda: self._upload("background"))
        self.apply_background_button.clicked.connect(self._apply_background)
        self.delete_background_button.clicked.connect(lambda: self._delete("background"))
        self.upload_sticker_button.clicked.connect(lambda: self._upload("sticker"))
        self.apply_sticker_button.clicked.connect(self._apply_sticker)
        self.delete_sticker_button.clicked.connect(lambda: self._delete("sticker"))
        self.background_mode_combo.currentIndexChanged.connect(self._background_scope_changed)
        self.background_page_combo.currentIndexChanged.connect(self._sync_background_selection)
        self.background_combo.currentIndexChanged.connect(self._update_previews)
        self.sticker_combo.currentIndexChanged.connect(self._update_previews)
        self.gmail_save_recipient_button.clicked.connect(self._save_gmail_recipient)
        self.gmail_connect_button.clicked.connect(lambda: self._connect_gmail(False))
        self.gmail_reauthorize_button.clicked.connect(lambda: self._connect_gmail(True))
        self.gmail_disconnect_button.clicked.connect(self._disconnect_gmail)
        self.refresh()

    def refresh(self) -> None:
        try:
            appearance = self.services.assets.appearance()
            backgrounds = self.services.assets.list_assets("background")
            stickers = self.services.assets.list_assets("sticker")
        except ServiceError as exc:
            self._show_error("無法載入個人化設定", exc)
            return

        blockers = (
            QSignalBlocker(self.background_mode_combo),
            QSignalBlocker(self.background_page_combo),
            QSignalBlocker(self.background_combo),
            QSignalBlocker(self.sticker_combo),
            QSignalBlocker(self.sticker_enabled_check),
        )
        _set_combo_data(self.background_mode_combo, appearance.background_mode)
        current_page = self.background_page_combo.currentData() or "reviews"
        _fill_asset_combo(self.background_combo, backgrounds, "不使用自訂背景")
        selected_background = (
            appearance.global_background_id
            if appearance.background_mode == "global"
            else appearance.page_background_ids.get(str(current_page))
        )
        _set_combo_data(self.background_combo, selected_background)
        _fill_asset_combo(self.sticker_combo, stickers, "不使用貼圖")
        _set_combo_data(self.sticker_combo, appearance.sticker_asset_id)
        self.sticker_enabled_check.setChecked(appearance.sticker_enabled)
        del blockers
        self._background_scope_changed()
        self._update_previews()
        self._refresh_gmail()

    def _refresh_gmail(self) -> None:
        try:
            status = self.services.gmail.status()
            recipient = self.services.gmail.default_recipient()
        except ServiceError as exc:
            self.gmail_account_status.setText(str(exc))
            self._set_gmail_buttons(False, False, False)
            return
        if not self.gmail_recipient_edit.hasFocus():
            self.gmail_recipient_edit.setText(recipient)
        if not status.configured:
            self.gmail_account_status.setText(
                "此版本尚未開放 Gmail。請等待支援 Gmail 的版本；"
                "你不需要準備設定檔，目前仍可使用本機 ZIP 備份。"
            )
            self._set_gmail_buttons(False, False, False)
        elif status.linked:
            self.gmail_account_status.setText(
                f"已連結：{status.account_email}。可到「資料與備份」手動寄送備份。"
            )
            self._set_gmail_buttons(False, True, True)
        else:
            self.gmail_account_status.setText(
                "尚未連結。點「連結 Gmail」，再到 Google 頁面完成登入授權。"
            )
            self._set_gmail_buttons(True, False, False)

    def _save_gmail_recipient(self) -> None:
        try:
            saved = self.services.gmail.set_default_recipient(self.gmail_recipient_edit.text())
        except ServiceError as exc:
            self._show_error("無法保存預設收件人", exc)
            return
        self.gmail_recipient_edit.setText(saved)
        self.status_label.setText("Gmail 預設收件人已保存。")

    def _connect_gmail(self, reauthorize: bool) -> None:
        QMessageBox.information(
            self,
            "Gmail 授權",
            "接下來會使用系統預設瀏覽器開啟 Google 官方授權頁。\n\n"
            "Task Assignment 不會要求或保存 Gmail 密碼。",
        )
        operation = self.services.gmail.reauthorize if reauthorize else self.services.gmail.connect
        self._start_gmail_account_operation(operation)

    def _disconnect_gmail(self) -> None:
        if (
            QMessageBox.question(
                self,
                "解除 Gmail 連結",
                "確定移除這台電腦保存的 Gmail 授權嗎？",
            )
            is not QMessageBox.StandardButton.Yes
        ):
            return
        self._start_gmail_account_operation(self.services.gmail.disconnect)

    def _save_motion(self) -> None:
        try:
            self.services.settings.set("interface_motion", self.motion_combo.currentData())
        except ServiceError as exc:
            self._show_error("無法儲存動畫設定", exc)
            return
        self.appearance_changed.emit()
        self._preview_motion()

    def _save_scale(self) -> None:
        try:
            self.services.settings.set("interface_scale_mode", self.scale_combo.currentData())
        except ServiceError as exc:
            self._show_error("無法儲存介面縮放", exc)
            return
        self.status_label.setText("介面縮放已套用；文字、按鈕與卡片會同步調整。")
        self.appearance_changed.emit()

    def _preview_motion(self) -> None:
        from task_assignment.ui.learning_visuals import CompletionFeedback, motion_mode

        for feedback in self.scroll.viewport().findChildren(CompletionFeedback):
            feedback.hide()
            feedback.deleteLater()
        CompletionFeedback(self.scroll.viewport(), "效果預覽 · 不會變更任務", motion_mode(self))

    def _start_gmail_account_operation(self, operation: Callable[[], object]) -> None:
        if self._gmail_thread is not None:
            return
        if not self._begin_operation("gmail_account"):
            return
        self._set_gmail_buttons(False, False, False)
        self.gmail_account_status.setText("正在處理 Gmail 帳號授權…")
        thread = QThread(self)
        worker = _OperationWorker(operation)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._gmail_account_succeeded)
        worker.failed.connect(self._gmail_account_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._gmail_account_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._gmail_thread = thread
        self._gmail_worker = worker
        thread.start()

    @Slot(object)
    def _gmail_account_succeeded(self, result: object) -> None:
        warning = result.warning if isinstance(result, GmailDisconnectResult) else None
        status = result.status if isinstance(result, GmailDisconnectResult) else result
        if isinstance(status, GmailAccountStatus) and status.linked:
            self.status_label.setText(f"已連結 Gmail：{status.account_email}")
        else:
            self.status_label.setText("Gmail 連結已移除。")
        if warning:
            QMessageBox.warning(self, "Gmail 解除連結", warning)

    @Slot(object)
    def _gmail_account_failed(self, error: object) -> None:
        message = str(error) if isinstance(error, ServiceError) else "Gmail 帳號操作失敗。"
        QMessageBox.warning(self, "Gmail 帳號操作失敗", message)

    @Slot()
    def _gmail_account_thread_finished(self) -> None:
        self._gmail_worker = None
        self._gmail_thread = None
        self._end_operation()
        self._refresh_gmail()

    def _set_gmail_buttons(self, connect: bool, reauthorize: bool, disconnect: bool) -> None:
        idle = self._gmail_thread is None
        self.gmail_connect_button.setEnabled(connect and idle)
        self.gmail_reauthorize_button.setEnabled(reauthorize and idle)
        self.gmail_disconnect_button.setEnabled(disconnect and idle)

    def _background_scope_changed(self) -> None:
        page_mode = self.background_mode_combo.currentData() == "page"
        self.background_page_label.setVisible(page_mode)
        self.background_page_combo.setVisible(page_mode)
        self._sync_background_selection()

    def _sync_background_selection(self) -> None:
        try:
            appearance = self.services.assets.appearance()
        except ServiceError as exc:
            self._show_error("無法載入背景設定", exc)
            return
        if self.background_mode_combo.currentData() == "global":
            asset_id = appearance.global_background_id
        else:
            asset_id = appearance.page_background_ids.get(
                str(self.background_page_combo.currentData())
            )
        blocker = QSignalBlocker(self.background_combo)
        _set_combo_data(self.background_combo, asset_id)
        del blocker
        self._update_previews()

    def _upload(self, kind: str) -> None:
        label = "背景" if kind == "background" else "貼圖"
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"上傳{label}",
            "",
            "圖片 (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        try:
            asset = self.services.assets.import_asset(path, kind)
        except ServiceError as exc:
            self._show_error(f"無法上傳{label}", exc)
            return
        self.refresh()
        combo = self.background_combo if kind == "background" else self.sticker_combo
        _set_combo_data(combo, asset.id)
        self.status_label.setText(f"已上傳「{asset.display_name}」，請按套用完成設定。")

    def _apply_background(self) -> None:
        mode = str(self.background_mode_combo.currentData())
        page_id = str(self.background_page_combo.currentData()) if mode == "page" else None
        try:
            self.services.assets.configure_background(
                self.background_combo.currentData(), mode=mode, page_id=page_id
            )
        except ServiceError as exc:
            self._show_error("無法套用背景", exc)
            return
        self.status_label.setText("背景設定已套用。")
        self.appearance_changed.emit()
        self.refresh()

    def _apply_sticker(self) -> None:
        try:
            self.services.assets.configure_sticker(
                self.sticker_combo.currentData(), enabled=self.sticker_enabled_check.isChecked()
            )
        except ServiceError as exc:
            self._show_error("無法套用貼圖", exc)
            return
        self.status_label.setText("貼圖設定已套用。")
        self.appearance_changed.emit()
        self.refresh()

    def _delete(self, kind: str) -> None:
        combo = self.background_combo if kind == "background" else self.sticker_combo
        asset_id = combo.currentData()
        label = "背景" if kind == "background" else "貼圖"
        if asset_id is None:
            return
        if (
            QMessageBox.question(
                self,
                f"刪除{label}",
                f"確定刪除目前選取的{label}嗎？使用中的素材會自動恢復預設。",
            )
            is not QMessageBox.StandardButton.Yes
        ):
            return
        try:
            self.services.assets.delete_asset(int(asset_id))
        except ServiceError as exc:
            self._show_error(f"無法刪除{label}", exc)
            return
        self.status_label.setText(f"{label}已刪除。")
        self.appearance_changed.emit()
        self.refresh()

    def _update_previews(self) -> None:
        _set_asset_preview(
            self.background_preview, self.background_combo.currentData(), self.services
        )
        _set_asset_preview(self.sticker_preview, self.sticker_combo.currentData(), self.services)
        self.delete_background_button.setEnabled(self.background_combo.currentData() is not None)
        self.delete_sticker_button.setEnabled(self.sticker_combo.currentData() is not None)
        if self.sticker_combo.currentData() is None:
            self.sticker_enabled_check.setToolTip("請先選擇已上傳的貼圖，再勾選套用。")
        else:
            self.sticker_enabled_check.setToolTip("套用後會顯示在主視窗左側導覽列底部。")


class InfoPage(PageBase):
    def __init__(
        self, title: str, description: str, note: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(title, description, f"{description}\n\n{note}", parent)
        panel = QFrame()
        panel.setObjectName("infoPanel")
        panel_layout = QVBoxLayout(panel)
        note_label = QLabel(note)
        note_label.setWordWrap(True)
        panel_layout.addWidget(note_label)
        panel_layout.addStretch(1)
        self.root_layout.addWidget(panel, 1)


def _table(headers: tuple[str, ...]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _metric_chip(text: str, *, accent: str = "normal") -> QLabel:
    label = QLabel(text)
    label.setObjectName("metricChip")
    label.setProperty("accent", accent)
    return label


def _metric_card(title: str) -> tuple[QFrame, QLabel]:
    card = QFrame()
    card.setObjectName("metricCard")
    layout = QVBoxLayout(card)
    title_label = QLabel(title)
    title_label.setObjectName("metricTitle")
    value = QLabel("0")
    value.setObjectName("metricValue")
    layout.addWidget(title_label)
    layout.addWidget(value)
    return card, value


def _restore_combo(combo: QComboBox, value: object) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _set_combo_data(combo: QComboBox, value: object) -> None:
    index = combo.findData(value)
    combo.setCurrentIndex(index if index >= 0 else 0)


def _fill_asset_combo(combo: QComboBox, assets: tuple[AssetView, ...], empty_label: str) -> None:
    combo.clear()
    combo.addItem(empty_label, None)
    for asset in assets:
        suffix = "" if asset.available else "（檔案遺失）"
        combo.addItem(f"{asset.display_name}{suffix}", asset.id)


def _set_asset_preview(label: QLabel, asset_id: int | None, services: ApplicationServices) -> None:
    label.clear()
    if asset_id is None:
        label.setText("目前未選擇素材")
        return
    asset = next(
        (item for item in services.assets.list_assets() if item.id == asset_id),
        None,
    )
    if asset is None or not asset.available:
        label.setText("素材檔案無法使用")
        return
    pixmap = QPixmap(str(asset.absolute_path))
    if pixmap.isNull():
        label.setText("素材無法預覽")
        return
    target_size = QSize(480, 108) if label.objectName() == "assetPreview" else QSize(100, 100)
    label.setPixmap(
        pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    )


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
