"""Task editing and read-only detail dialogs."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDateTime, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QBoxLayout,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from task_assignment.application import (
    ServiceError,
    TaskDetails,
    TaskDraft,
    TaskService,
)
from task_assignment.domain.enums import ScheduleMode, TaskStatus
from task_assignment.ui.visuals import scaled

DATE_TIME_FORMAT = "yyyy-MM-dd HH:mm"
PYTHON_DATE_TIME_FORMAT = "%Y-%m-%d %H:%M"
TASK_STATUS_TEXT = {
    TaskStatus.NOT_STARTED: "未開始",
    TaskStatus.IN_PROGRESS: "進行中",
    TaskStatus.DUE_TODAY: "今日待複習",
    TaskStatus.OVERDUE: "逾期",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.INCOMPLETE: "未完全完成",
    TaskStatus.PAUSED: "已暫停",
    TaskStatus.ARCHIVED: "已封存",
}


class TaskDialog(QDialog):
    """Create/edit/copy form with live schedule preview."""

    def __init__(
        self,
        service: TaskService,
        *,
        details: TaskDetails | None = None,
        copy_mode: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.details = details
        self.copy_mode = copy_mode
        self.result_draft: TaskDraft | None = None
        self.setModal(True)
        self.setMinimumSize(520, 420)
        available = self.screen().availableGeometry()
        self.resize(min(1020, available.width() - 60), min(780, available.height() - 60))
        self.setWindowTitle("複製任務" if copy_mode else ("編輯任務" if details else "新增任務"))

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        heading = QLabel(self.windowTitle())
        heading.setObjectName("dialogTitle")
        root.addWidget(heading)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("taskDialogScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content_root = QVBoxLayout(content)
        content_root.setContentsMargins(0, 0, 8, 0)
        content_root.setSpacing(8)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setSpacing(6)
        self.form = form

        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("taskNameEdit")
        self.name_edit.setPlaceholderText("例如：準備 Python 證照")
        form.addRow("任務名稱 *", self.name_edit)

        self.description_edit = QPlainTextEdit()
        self.description_edit.setObjectName("taskDescriptionEdit")
        self.description_edit.setPlaceholderText("補充筆記（選填，可隨內容增高）")
        self.description_edit.setMinimumHeight(scaled(56))
        self.description_edit.setMaximumHeight(scaled(220))
        self.description_edit.setFixedHeight(scaled(56))
        self.description_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.description_edit.textChanged.connect(self._resize_description)
        form.addRow("說明／筆記", self.description_edit)

        self.category_combo = QComboBox()
        self.category_combo.setObjectName("taskCategoryCombo")
        category_row = QHBoxLayout()
        category_row.addWidget(self.category_combo, 1)
        self.add_category_button = QPushButton("＋ 新分類")
        self.add_category_button.clicked.connect(lambda: self._create_catalog("category"))
        category_row.addWidget(self.add_category_button)
        form.addRow("分類", category_row)

        self.tag_list = QListWidget()
        self.tag_list.setObjectName("taskTagList")
        self.tag_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.tag_list.setFixedHeight(56)
        tag_panel = QVBoxLayout()
        tag_hint = QLabel("選填，勾選可加入多個標籤。例如「考試」「重點」，供搜尋與篩選。")
        tag_hint.setWordWrap(True)
        tag_panel.addWidget(tag_hint)
        tag_row = QHBoxLayout()
        tag_row.addWidget(self.tag_list, 1)
        self.add_tag_button = QPushButton("＋ 新標籤")
        self.add_tag_button.clicked.connect(lambda: self._create_catalog("tag"))
        tag_row.addWidget(self.add_tag_button)
        tag_panel.addLayout(tag_row)
        form.addRow("標籤", tag_panel)

        self.start_edit = QDateTimeEdit()
        self.start_edit.setObjectName("taskStartEdit")
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDisplayFormat(DATE_TIME_FORMAT)
        self.start_edit.setDateTime(QDateTime(datetime.now().replace(second=0, microsecond=0)))
        form.addRow("開始時間 *", self.start_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.setObjectName("taskScheduleModeCombo")
        self.mode_combo.addItem("遺忘曲線", ScheduleMode.CURVE.value)
        self.mode_combo.addItem("自訂日期（用日曆選擇）", ScheduleMode.MANUAL.value)
        form.addRow("排程方式 *", self.mode_combo)

        self.review_count_spin = QSpinBox()
        self.review_count_spin.setObjectName("taskReviewCountSpin")
        self.review_count_spin.setRange(3, 10)
        self.review_count_spin.setValue(3)
        self.review_count_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.count_panel = QWidget()
        count_row = QHBoxLayout(self.count_panel)
        count_row.setContentsMargins(0, 0, 0, 0)
        self.less_count_button = QPushButton("減少")
        self.more_count_button = QPushButton("增加")
        self.less_count_button.setAccessibleName("減少複習次數")
        self.more_count_button.setAccessibleName("增加複習次數")
        self.less_count_button.clicked.connect(self.review_count_spin.stepDown)
        self.more_count_button.clicked.connect(self.review_count_spin.stepUp)
        count_row.addWidget(self.less_count_button)
        count_row.addWidget(self.review_count_spin)
        count_row.addWidget(self.more_count_button)
        form.addRow("複習次數", self.count_panel)

        self.manual_panel = QWidget()
        manual_layout = QVBoxLayout(self.manual_panel)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        picker_row = QHBoxLayout()
        self.manual_picker = QDateTimeEdit(self.start_edit.dateTime().addDays(1))
        self.manual_picker.setCalendarPopup(True)
        self.manual_picker.setDisplayFormat(DATE_TIME_FORMAT)
        self.manual_add_button = QPushButton("加入日期")
        self.manual_add_button.clicked.connect(self._add_manual_time)
        picker_row.addWidget(self.manual_picker, 1)
        picker_row.addWidget(self.manual_add_button)
        manual_layout.addLayout(picker_row)
        self.manual_list = QListWidget()
        self.manual_list.setFixedHeight(70)
        manual_layout.addWidget(self.manual_list)
        self.manual_remove_button = QPushButton("移除選取日期")
        self.manual_remove_button.clicked.connect(self._remove_manual_time)
        self.manual_list.itemSelectionChanged.connect(self._update_manual_actions)
        self.manual_picker.dateTimeChanged.connect(self._update_manual_actions)
        manual_layout.addWidget(self.manual_remove_button)
        form.addRow("自訂複習時間", self.manual_panel)
        content_root.addLayout(form)
        content_root.addStretch(1)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_panel.setMinimumWidth(240)
        preview_heading = QLabel("排程預覽")
        preview_heading.setObjectName("sectionTitle")
        preview_layout.addWidget(preview_heading)
        self.preview_list = QListWidget()
        self.preview_list.setObjectName("schedulePreviewList")
        self.preview_list.setMinimumHeight(130)
        preview_layout.addWidget(self.preview_list, 1)
        self.validation_label = QLabel()
        self.validation_label.setObjectName("validationMessage")
        self.validation_label.setWordWrap(True)
        preview_layout.addWidget(self.validation_label)
        self.scroll.setWidget(content)
        columns = QHBoxLayout()
        self.columns = columns
        self.preview_panel = preview_panel
        columns.addWidget(self.scroll, 3)
        columns.addWidget(preview_panel, 2)
        root.addLayout(columns, 1)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.button(QDialogButtonBox.StandardButton.Save).setText("儲存")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        root.addWidget(self.button_box)

        self._load_options()
        if details is not None:
            self._load_details(details, copy_mode=copy_mode)
        self._connect_preview_signals()
        self._update_mode_fields()
        self._resize_description()
        self.refresh_preview()

    def _resize_description(self) -> None:
        """Grow notes as they are typed, while keeping the form scrollable."""

        document_height = self.description_edit.document().documentLayout().documentSize().height()
        target = max(scaled(56), min(scaled(220), int(document_height) + scaled(18)))
        if self.description_edit.height() != target:
            self.description_edit.setFixedHeight(target)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "columns"):
            compact = self.width() < 860
            self.columns.setDirection(
                QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
            )
            self.preview_panel.setMaximumHeight(180 if compact else 16777215)

    def _load_options(self) -> None:
        self.category_combo.addItem("未分類", None)
        for category in self.service.categories():
            self.category_combo.addItem(category.name, category.id)
        for tag in self.service.tags():
            item = QListWidgetItem(tag.name)
            item.setData(Qt.ItemDataRole.UserRole, tag.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.tag_list.addItem(item)

    def _load_details(self, details: TaskDetails, *, copy_mode: bool) -> None:
        task = details.task
        self.name_edit.setText(f"{task.name}（副本）" if copy_mode else task.name)
        self.description_edit.setPlainText(task.description)
        category_index = self.category_combo.findData(
            task.category.id if task.category is not None else None
        )
        self.category_combo.setCurrentIndex(max(category_index, 0))
        selected_tag_ids = {tag.id for tag in task.tags}
        for index in range(self.tag_list.count()):
            item = self.tag_list.item(index)
            item.setCheckState(
                Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in selected_tag_ids
                else Qt.CheckState.Unchecked
            )
        self.start_edit.setDateTime(QDateTime(task.start_at))
        mode_index = self.mode_combo.findData(task.schedule_mode.value)
        self.mode_combo.setCurrentIndex(max(mode_index, 0))
        if task.review_count is not None:
            self.review_count_spin.setValue(task.review_count)
        if task.schedule_mode is ScheduleMode.MANUAL:
            for schedule in details.schedules:
                item = QListWidgetItem(schedule.scheduled_at.strftime(PYTHON_DATE_TIME_FORMAT))
                item.setData(Qt.ItemDataRole.UserRole, schedule.scheduled_at)
                self.manual_list.addItem(item)

    def _connect_preview_signals(self) -> None:
        self.name_edit.textChanged.connect(self.refresh_preview)
        self.start_edit.dateTimeChanged.connect(self.refresh_preview)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.review_count_spin.valueChanged.connect(self.refresh_preview)
        self.start_edit.dateTimeChanged.connect(self._update_manual_actions)
        self.review_count_spin.valueChanged.connect(self._update_mode_fields)

    def _on_mode_changed(self) -> None:
        self._update_mode_fields()
        self.refresh_preview()

    def _update_mode_fields(self) -> None:
        is_curve = self.mode_combo.currentData() == ScheduleMode.CURVE.value
        self.form.setRowVisible(self.count_panel, is_curve)
        self.form.setRowVisible(self.manual_panel, not is_curve)
        self.less_count_button.setEnabled(self.review_count_spin.value() > 3)
        self.more_count_button.setEnabled(self.review_count_spin.value() < 10)
        self._update_manual_actions()

    def _create_catalog(self, kind: str) -> None:
        label = "分類" if kind == "category" else "標籤"
        name, accepted = QInputDialog.getText(
            self, f"新增{label}", f"{label}名稱（建立後可供其他任務使用）"
        )
        if not accepted or not name.strip():
            return
        try:
            entry = (self.service.create_category(name) if kind == "category"
                     else self.service.create_tag(name))
        except ServiceError as exc:
            QMessageBox.warning(self, f"無法新增{label}", str(exc))
            return
        if kind == "category":
            self.category_combo.addItem(entry.name, entry.id)
            self.category_combo.setCurrentIndex(self.category_combo.count() - 1)
        else:
            item = QListWidgetItem(entry.name)
            item.setData(Qt.ItemDataRole.UserRole, entry.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.tag_list.addItem(item)

    def _update_manual_actions(self) -> None:
        self.manual_picker.setMinimumDateTime(self.start_edit.dateTime())
        selected = self.manual_picker.dateTime().toPython()
        duplicate = selected in self._parse_manual_times()
        self.manual_add_button.setEnabled(not duplicate)
        self.manual_add_button.setToolTip(
            "此時間已加入，請選擇其他日期。" if duplicate else "加入一筆複習"
        )
        self.manual_remove_button.setEnabled(self.manual_list.currentRow() >= 0)

    def _add_manual_time(self) -> None:
        value = self.manual_picker.dateTime().toPython()
        if value in self._parse_manual_times():
            return
        item = QListWidgetItem(value.strftime(PYTHON_DATE_TIME_FORMAT))
        item.setData(Qt.ItemDataRole.UserRole, value)
        self.manual_list.addItem(item)
        self.manual_list.sortItems()
        self._update_manual_actions()
        self.refresh_preview()

    def _remove_manual_time(self) -> None:
        row = self.manual_list.currentRow()
        if row >= 0:
            self.manual_list.takeItem(row)
        self._update_manual_actions()
        self.refresh_preview()

    def draft(self) -> TaskDraft:
        try:
            mode = ScheduleMode(self.mode_combo.currentData())
        except (TypeError, ValueError) as exc:
            raise ValueError("找不到排程方式。") from exc
        tag_ids = tuple(
            int(self.tag_list.item(index).data(Qt.ItemDataRole.UserRole))
            for index in range(self.tag_list.count())
            if self.tag_list.item(index).checkState() == Qt.CheckState.Checked
        )
        return TaskDraft(
            name=self.name_edit.text(),
            description=self.description_edit.toPlainText(),
            category_id=self.category_combo.currentData(),
            tag_ids=tag_ids,
            start_at=self.start_edit.dateTime().toPython(),
            schedule_mode=mode,
            review_count=self.review_count_spin.value() if mode is ScheduleMode.CURVE else None,
            manual_schedule_times=(
                self._parse_manual_times() if mode is ScheduleMode.MANUAL else ()
            ),
        )

    def _parse_manual_times(self) -> tuple[datetime, ...]:
        return tuple(self.manual_list.item(index).data(Qt.ItemDataRole.UserRole)
                     for index in range(self.manual_list.count()))

    def refresh_preview(self) -> None:
        save_button = self.button_box.button(QDialogButtonBox.StandardButton.Save)
        try:
            preview = self.service.preview_schedule(self.draft())
        except (ServiceError, ValueError) as exc:
            self.preview_list.clear()
            self.validation_label.setText(str(exc))
            self.validation_label.setProperty("valid", False)
            save_button.setEnabled(False)
            self.validation_label.style().unpolish(self.validation_label)
            self.validation_label.style().polish(self.validation_label)
            return

        with QSignalBlocker(self.preview_list):
            self.preview_list.clear()
            for sequence, scheduled_at in enumerate(preview.scheduled_at, start=1):
                self.preview_list.addItem(
                    f"第 {sequence} 次　{scheduled_at.strftime(PYTHON_DATE_TIME_FORMAT)}"
                )
        self.validation_label.setText(f"共 {len(preview.scheduled_at)} 次複習")
        self.validation_label.setProperty("valid", True)
        save_button.setEnabled(True)
        self.validation_label.style().unpolish(self.validation_label)
        self.validation_label.style().polish(self.validation_label)

    def accept(self) -> None:
        try:
            draft = self.draft()
            self.service.preview_schedule(draft)
            if self.details is not None and not self.copy_mode:
                changes = self.service.preview_change(self.details.task.id, draft)
                changed_count = len(changes.removed_pending) + len(changes.added_pending)
                if (
                    changed_count
                    and QMessageBox.question(
                        self,
                        "確認排程變更",
                        (
                            f"這次修改會保留 {len(changes.preserved_history)} 筆歷史，"
                            f"移除 {len(changes.removed_pending)} 筆、加入 "
                            f"{len(changes.added_pending)} 筆待處理排程。確定儲存嗎？"
                        ),
                    )
                    is not QMessageBox.StandardButton.Yes
                ):
                    return
        except (ServiceError, ValueError) as exc:
            QMessageBox.warning(self, "無法儲存任務", str(exc))
            return
        self.result_draft = draft
        super().accept()


class TaskDetailsDialog(QDialog):
    def __init__(self, details: TaskDetails, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("任務詳細資料")
        self.resize(620, 520)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        task = details.task

        heading = QLabel(task.name)
        heading.setObjectName("dialogTitle")
        root.addWidget(heading)
        metadata = QLabel(
            "　".join(
                (
                    f"狀態：{TASK_STATUS_TEXT[task.status]}",
                    f"完成率：{task.completion_rate:.0f}%",
                    f"分類：{task.category.name if task.category else '未分類'}",
                    f"標籤：{', '.join(tag.name for tag in task.tags) or '無'}",
                )
            )
        )
        metadata.setWordWrap(True)
        root.addWidget(metadata)
        from task_assignment.ui.learning_visuals import ReviewPath

        path_scroll = QScrollArea()
        path_scroll.setWidgetResizable(True)
        path_scroll.setFixedHeight(150)
        path_scroll.setWidget(ReviewPath(details.schedules))
        root.addWidget(path_scroll)
        if task.description:
            note = QPlainTextEdit()
            note.setObjectName("detailNote")
            note.setPlainText(task.description)
            note.setReadOnly(True)
            note.setMinimumHeight(scaled(56))
            note.setMaximumHeight(scaled(180))
            note.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            note.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            root.addWidget(note)

        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(("次數", "預定時間", "狀態"))
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)
        table.setRowCount(len(details.schedules))
        for row, schedule in enumerate(details.schedules):
            table.setItem(row, 0, QTableWidgetItem(str(schedule.sequence)))
            table.setItem(
                row,
                1,
                QTableWidgetItem(schedule.scheduled_at.strftime(PYTHON_DATE_TIME_FORMAT)),
            )
            table.setItem(
                row,
                2,
                QTableWidgetItem(
                    {
                        "pending": "待複習",
                        "completed": "已完成",
                        "skipped": "已略過",
                    }[schedule.status.value]
                ),
            )
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setStretchLastSection(False)
        root.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("關閉")
        root.addWidget(buttons)


class DateTimeDialog(QDialog):
    def __init__(
        self, value: datetime, *, title: str = "編輯複習時間", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        row = QHBoxLayout()
        row.addWidget(QLabel("新的複習時間"))
        self.date_time_edit = QDateTimeEdit(QDateTime(value))
        self.date_time_edit.setCalendarPopup(True)
        self.date_time_edit.setDisplayFormat(DATE_TIME_FORMAT)
        row.addWidget(self.date_time_edit, 1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("儲存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> datetime:
        return self.date_time_edit.dateTime().toPython()
