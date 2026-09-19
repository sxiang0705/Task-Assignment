"""Responsive application shell and primary navigation."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QPixmap, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from task_assignment.application.errors import ServiceError
from task_assignment.application.services import ApplicationServices
from task_assignment.config import AppPaths
from task_assignment.ui.learning_visuals import RevealEffect
from task_assignment.ui.pages import (
    BackupPage,
    CalendarPage,
    DashboardPage,
    PageBase,
    PersonalizationPage,
    TaskManagementPage,
    TodayTasksPage,
)
from task_assignment.ui.visuals import NavigationButton, scaled, theme_for_scale

NAVIGATION_ITEMS = (
    ("reviews", "今日任務", "處理今日與逾期複習項目。"),
    ("tasks", "任務管理", "建立、搜尋與管理學習任務。"),
    ("calendar", "月曆", "依日期查看複習安排。"),
    ("dashboard", "儀表板", "掌握今日、逾期與近期複習概況。"),
    ("backup", "資料與備份", "建立或還原 Task Assignment 備份。"),
    ("settings", "個人化設定", "調整背景、貼圖與應用程式設定。"),
)
DEFAULT_PAGE_ID = "reviews"


class AppShell(QMainWindow):
    """Top-level window with keyboard-accessible primary navigation."""

    def __init__(
        self,
        *,
        paths: AppPaths,
        services: ApplicationServices,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths
        self.services = services
        self._ui_scale = self._read_interface_scale()
        application = QApplication.instance()
        if application is not None:
            application.setProperty("taskAssignmentScale", self._ui_scale)
        self._operation_owner = None
        self._import_in_progress = False
        self._restart_pending = False
        self._last_local_date = services.tasks.now_provider().date()
        self.setObjectName("appShell")
        self.setWindowTitle("Task Assignment")
        self.setMinimumSize(720, 480)
        self.resize(1180, 760)

        root = QWidget(self)
        root.setObjectName("appRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        navigation = QFrame(root)
        navigation.setObjectName("primaryNavigation")
        navigation.setMinimumWidth(scaled(170))
        navigation.setMaximumWidth(scaled(240))
        navigation.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.navigation_frame = navigation
        self.active_indicator = QFrame(navigation)
        self.active_indicator.setStyleSheet("background: #b69bea; border-radius: 2px;")
        self.active_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.active_indicator.setGeometry(0, scaled(100), scaled(4), scaled(32))
        self.indicator_motion = QPropertyAnimation(self.active_indicator, b"geometry", self)
        self.indicator_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        navigation_layout = QVBoxLayout(navigation)
        navigation_layout.setContentsMargins(16, 20, 16, 20)
        navigation_layout.setSpacing(8)

        self.brand_label = QLabel("Task Assignment")
        self.brand_label.setWordWrap(True)
        self.brand_label.setObjectName("brandLabel")
        navigation_layout.addWidget(self.brand_label)
        navigation_layout.addSpacing(14)

        self.page_stack = QStackedWidget(root)
        self.navigation_group = QButtonGroup(self)
        self.navigation_group.setExclusive(True)
        self.navigation_buttons: dict[str, QPushButton] = {}
        self.page_widgets: dict[str, PageBase] = {}

        for index, (page_id, label, description) in enumerate(NAVIGATION_ITEMS):
            button = NavigationButton(label)
            button.setObjectName(f"nav_{page_id}")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setMinimumHeight(scaled(44))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(f"前往{label}")
            button.clicked.connect(lambda checked=False, value=index: self.show_page(value))
            self.navigation_group.addButton(button, index)
            self.navigation_buttons[page_id] = button
            navigation_layout.addWidget(button)
            page = self._create_page(page_id, label, description)
            page.data_changed.connect(self.refresh_all_pages)
            self.page_widgets[page_id] = page
            self.page_stack.addWidget(page)

        navigation_layout.addStretch(1)
        root_layout.addWidget(navigation)
        root_layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(root)
        self.sticker_label = QLabel(navigation)
        self.sticker_label.setObjectName("stickerOverlay")
        self.sticker_label.setFixedSize(scaled(112), scaled(112))
        self.sticker_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sticker_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.sticker_label.hide()
        dashboard = self.page_widgets["dashboard"]
        if isinstance(dashboard, DashboardPage):
            dashboard.navigate_requested.connect(self.show_page_by_id)
        reviews = self.page_widgets["reviews"]
        if isinstance(reviews, TodayTasksPage):
            reviews.manage_task_requested.connect(self._manage_task)
        personalization = self.page_widgets["settings"]
        if isinstance(personalization, PersonalizationPage):
            personalization.appearance_changed.connect(self.apply_appearance)
        backup = self.page_widgets["backup"]
        if isinstance(backup, BackupPage):
            backup.restart_required.connect(self._lock_for_restart)
        self._apply_style()
        self.apply_appearance()
        default_index = next(
            index for index, item in enumerate(NAVIGATION_ITEMS) if item[0] == DEFAULT_PAGE_ID
        )
        # Page constructors have already loaded their data; do not query Today twice.
        self.show_page(default_index, refresh=False)
        self.day_timer = QTimer(self)
        self.day_timer.setInterval(30_000)
        self.day_timer.timeout.connect(self._check_date_change)
        self.day_timer.start()

    def show_page(self, index: int, *, refresh: bool = True) -> None:
        if self._import_in_progress or self._restart_pending:
            return
        if not 0 <= index < self.page_stack.count():
            raise IndexError(f"Unknown page index: {index}")
        previous_index = self.page_stack.currentIndex()
        self.page_stack.setCurrentIndex(index)
        button = self.navigation_group.button(index)
        if button is not None:
            button.setChecked(True)
            self._move_indicator(button)
        page = self.page_stack.widget(index)
        if refresh and isinstance(page, PageBase):
            page.refresh()
        if previous_index != index and self.isVisible() and self._motion_duration:
            if hasattr(self, "_page_motion"):
                self._page_motion.stop()
                self._page_effect.setOpacity(1)
                self._page_motion.deleteLater()
            effect = page.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = RevealEffect(page)
                page.setGraphicsEffect(effect)
            if isinstance(effect, RevealEffect):
                effect.distance = 10 if self._motion_duration > 100 else 0
            self._page_effect = effect
            self._page_motion = QPropertyAnimation(effect, b"opacity", self)
            self._page_motion.setDuration(self._motion_duration)
            self._page_motion.setStartValue(0.35)
            self._page_motion.setEndValue(1.0)
            self._page_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._page_motion.start()

    def show_page_by_id(self, page_id: str) -> None:
        if page_id not in self.page_widgets:
            raise KeyError(f"Unknown page: {page_id}")
        self.show_page(tuple(self.page_widgets).index(page_id))

    def _move_indicator(self, button) -> None:
        target = button.geometry().adjusted(-button.x(), 6, 0, -6)
        target.setWidth(4)
        self.indicator_motion.stop()
        self.indicator_motion.setDuration(getattr(self, "_motion_duration", 0))
        self.indicator_motion.setStartValue(self.active_indicator.geometry())
        self.indicator_motion.setEndValue(target)
        self.indicator_motion.start()
        self.active_indicator.raise_()

    def refresh_all_pages(self) -> None:
        if self._import_in_progress or self._restart_pending:
            return
        for page in self.page_widgets.values():
            page.refresh()

    def try_start_operation(self, owner: QWidget, kind: str) -> bool:
        if self._restart_pending or self._operation_owner not in (None, owner):
            self.statusBar().showMessage("請先完成目前的備份或 Gmail 操作。", 5000)
            return False
        self._operation_owner = owner
        self._import_in_progress = kind == "import"
        if self._import_in_progress:
            self.navigation_frame.setEnabled(False)
            for page in self.page_widgets.values():
                if page is not owner:
                    page.setEnabled(False)
        return True

    def finish_operation(self, owner: QWidget) -> None:
        if self._operation_owner is not owner:
            return
        self._operation_owner = None
        self._import_in_progress = False
        if not self._restart_pending:
            self.navigation_frame.setEnabled(True)
            for page in self.page_widgets.values():
                page.setEnabled(True)
            self._check_date_change()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._operation_owner is not None:
            event.ignore()
            self.statusBar().showMessage("背景工作尚未結束，請完成或取消目前操作後再關閉。", 8000)
            return
        super().closeEvent(event)
        if event.isAccepted():
            self.day_timer.stop()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate and hasattr(self, "day_timer"):
            QTimer.singleShot(0, self._check_date_change)
        return super().event(event)

    def _check_date_change(self) -> None:
        if self._operation_owner is not None or self._restart_pending:
            return
        today = self.services.tasks.now_provider().date()
        if today == self._last_local_date:
            return
        try:
            self.services.tasks.refresh_all_statuses()
            self.refresh_all_pages()
        except ServiceError:
            self.statusBar().showMessage("跨日更新暫時失敗，稍後會自動重試。", 5000)
            return
        self._last_local_date = today

    def _manage_task(self, task_id: int) -> None:
        self.show_page_by_id("tasks")
        page = self.page_widgets["tasks"]
        if isinstance(page, TaskManagementPage):
            page.select_task(task_id)

    def _create_page(self, page_id: str, title: str, description: str) -> PageBase:
        if page_id == "reviews":
            return TodayTasksPage(self.services)
        if page_id == "tasks":
            return TaskManagementPage(self.services)
        if page_id == "calendar":
            return CalendarPage(self.services)
        if page_id == "dashboard":
            return DashboardPage(self.services)
        if page_id == "backup":
            return BackupPage(self.services)
        if page_id == "settings":
            return PersonalizationPage(self.services)
        raise KeyError(f"Unknown page: {page_id}")

    def _lock_for_restart(self, _result: object) -> None:
        self._restart_pending = True
        self.setWindowTitle("Task Assignment — 請重新啟動")
        self.page_stack.setEnabled(False)
        self.navigation_frame.setEnabled(False)

    def apply_appearance(self) -> None:
        mode = self.services.settings.get("interface_motion", "full")
        self._ui_scale = self._read_interface_scale()
        application = QApplication.instance()
        if application is not None:
            application.setProperty("taskAssignmentScale", self._ui_scale)
        self._apply_style()
        self.navigation_frame.setMinimumWidth(scaled(170))
        self.navigation_frame.setMaximumWidth(scaled(240))
        for button in self.navigation_buttons.values():
            button.setMinimumHeight(scaled(44))
        self.sticker_label.setFixedSize(scaled(112), scaled(112))
        self._motion_duration = {"full": 210, "reduced": 90, "off": 0}.get(mode, 210)
        self.indicator_motion.stop()
        for widget in self.findChildren(QWidget):
            animation = getattr(widget, "_reveal_motion", None)
            if animation is not None:
                animation.stop()
                widget.graphicsEffect().setOpacity(1)
        from task_assignment.ui.learning_visuals import CardDeparture, CompletionFeedback

        for departure in self.findChildren(CardDeparture):
            departure.finish()
        for feedback in self.findChildren(CompletionFeedback):
            feedback.hide()
            feedback.deleteLater()
        hero = self.page_widgets["reviews"].hero
        if mode != "full":
            hero.animation.stop()
            hero._step(float(hero.today))
        if hasattr(self, "_page_motion"):
            self._page_motion.stop()
            self._page_effect.setOpacity(1)
        for button in self.navigation_buttons.values():
            button.motion.setDuration(min(150, self._motion_duration))
            button._retarget()
        calendar_page = self.page_widgets["calendar"]
        calendar_page.calendar._hover_motion.setDuration(min(150, self._motion_duration))
        calendar_page.calendar._hover_motion.stop()
        calendar_page.calendar._animate_hover(120)
        for page_id, page in self.page_widgets.items():
            try:
                asset = self.services.assets.background_for(page_id)
            except ServiceError:
                asset = None
            page.set_background(
                asset.absolute_path if asset is not None and asset.available else None
            )
        try:
            sticker = self.services.assets.sticker()
        except ServiceError:
            sticker = None
        if sticker is None or not sticker.available:
            self.sticker_label.clear()
            self.sticker_label.hide()
            return
        pixmap = QPixmap(str(sticker.absolute_path))
        if pixmap.isNull():
            self.sticker_label.clear()
            self.sticker_label.hide()
            return
        self.sticker_label.setPixmap(
            pixmap.scaled(
                self.sticker_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._position_sticker()
        self.sticker_label.show()
        self.sticker_label.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_sticker()
        reviews = self.page_widgets.get("reviews")
        if reviews is not None:
            reviews.hero.setVisible(self.height() >= 650)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if hasattr(self, "day_timer"):
            self.day_timer.start()
        self._move_indicator(self.navigation_group.checkedButton())
        self._position_sticker()
        if self.sticker_label.isVisible():
            self.sticker_label.raise_()

    def _position_sticker(self) -> None:
        if not hasattr(self, "sticker_label"):
            return
        margin = 18
        self.sticker_label.move(
            (self.navigation_frame.width() - self.sticker_label.width()) // 2,
            self.navigation_frame.height() - self.sticker_label.height() - margin,
        )

    def _apply_style(self) -> None:
        self.setStyleSheet(theme_for_scale(self._ui_scale))

    def _read_interface_scale(self) -> float:
        value = self.services.settings.get("interface_scale", 1.0)
        try:
            scale = float(value)
        except (TypeError, ValueError):
            scale = 1.0
        return min(1.5, max(0.9, scale))
