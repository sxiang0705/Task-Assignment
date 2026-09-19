"""Shared visual language and lightweight, interruptible motion."""

from PySide6.QtCore import QEasingCurve, QRect, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QPushButton, QStyle, QStyledItemDelegate


def interface_scale(widget=None) -> float:
    """Return the user-selected logical UI scale without relying on OS DPI."""

    application = QApplication.instance()
    value = application.property("taskAssignmentScale") if application is not None else None
    try:
        scale = float(value)
    except (TypeError, ValueError):
        scale = 1.0
    return min(2.0, max(0.8, scale))


def scaled(value: int, widget=None) -> int:
    """Scale a fixed logical dimension used by custom-painted controls."""

    return max(1, round(value * interface_scale(widget)))


class NavigationButton(QPushButton):
    """Paint an animated surface without animating layout geometry."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._surface = QColor("#202840")
        self._hovered = False
        self.motion = QVariantAnimation(self)
        self.motion.setDuration(150)
        self.motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.motion.valueChanged.connect(self._set_surface)
        self.toggled.connect(self._retarget)

    def _set_surface(self, color):
        self._surface = color
        self.update()

    def _retarget(self, *_):
        target = QColor(
            "#ddd6ff" if self.isChecked() else "#343e5c" if self._hovered else "#202840"
        )
        self.motion.stop()
        if not self.isVisible() or self.motion.duration() == 0:
            self._set_surface(target)
            return
        self.motion.setStartValue(self._surface)
        self.motion.setEndValue(target)
        self.motion.start()

    def enterEvent(self, event):
        self._hovered = True
        self._retarget()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._retarget()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#afa0ed"), 2) if self.hasFocus() else Qt.PenStyle.NoPen)
        painter.setBrush(self._surface.darker(110) if self.isDown() else self._surface)
        painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 12, 12)
        painter.setPen(QColor("#252d49" if self._surface.lightness() > 140 else "#e2e5f1"))
        font = QFont(self.font())
        font.setBold(self.isChecked())
        painter.setFont(font)
        icon = QRect(15, (self.height() - 18) // 2, 18, 18)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(icon, 4, 4)
        if self.isChecked():
            painter.drawLine(icon.left() + 4, icon.center().y(), icon.left() + 8, icon.bottom() - 4)
            painter.drawLine(icon.left() + 8, icon.bottom() - 4, icon.right() - 3, icon.top() + 4)
        else:
            painter.drawLine(icon.left() + 5, icon.top() + 6, icon.right() - 4, icon.top() + 6)
            painter.drawLine(icon.left() + 5, icon.top() + 11, icon.right() - 4, icon.top() + 11)
        painter.drawText(
            self.rect().adjusted(45, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter, self.text()
        )


class ReviewCardDelegate(QStyledItemDelegate):
    """Render bounded review rows as cards, without one widget per task."""

    def sizeHint(self, option, index):
        description = str(index.siblingAtColumn(3).data() or "")
        extra_lines = min(2, max(0, description.count("\n")))
        return QSize(scaled(320, option.widget), scaled(128 + extra_lines * 18, option.widget))

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        rect = option.rect.adjusted(scaled(3, option.widget), scaled(5, option.widget),
                                    -scaled(3, option.widget), -scaled(5, option.widget))
        painter.setBrush(QColor("#eee9ff" if selected else "#f7f4ff" if hovered else "#ffffff"))
        painter.setPen(QPen(QColor("#9c88dd" if selected else "#e6e3ed"), 1))
        painter.drawRoundedRect(rect, 14, 14)
        values = [str(index.siblingAtColumn(col).data() or "") for col in range(7)]
        text_rect = rect.adjusted(scaled(22, option.widget), scaled(12, option.widget),
                                  -scaled(22, option.widget), -scaled(12, option.widget))
        font = QFont(option.font)
        font.setPixelSize(scaled(17, option.widget))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#252d49"))
        title = painter.fontMetrics().elidedText(
            values[1], Qt.TextElideMode.ElideRight, max(30, text_rect.width() - 85)
        )
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignTop, title)
        font.setPixelSize(scaled(12, option.widget))
        painter.setFont(font)
        painter.setPen(QColor("#bd5c62" if values[0] == "逾期" else "#7663ba"))
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, values[0]
        )
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor("#72788b"))
        subtitle = f"{values[5]}  ·  {values[4]}  ·  {values[2]}"
        subtitle = painter.fontMetrics().elidedText(
            subtitle, Qt.TextElideMode.ElideRight, text_rect.width()
        )
        painter.drawText(text_rect.adjusted(0, scaled(31, option.widget), 0, 0),
                         Qt.AlignmentFlag.AlignTop, subtitle)
        note_rect = text_rect.adjusted(0, scaled(52, option.widget), 0,
                                       -scaled(26, option.widget))
        description = painter.fontMetrics().elidedText(
            values[3].replace("\n", " "), Qt.TextElideMode.ElideRight, note_rect.width()
        )
        painter.drawText(note_rect, Qt.AlignmentFlag.AlignTop, description)
        track = QRect(text_rect.left(), text_rect.bottom() - scaled(6, option.widget),
                      max(scaled(20, option.widget), text_rect.width() - scaled(55, option.widget)),
                      scaled(4, option.widget))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#eeeaf7"))
        painter.drawRoundedRect(track, scaled(2, option.widget), scaled(2, option.widget))
        progress = float(values[6].rstrip("%") or "0") / 100
        painter.setBrush(QColor("#9b89da"))
        painter.drawRoundedRect(
            QRect(track.x(), track.y(), int(track.width() * progress), track.height()),
            scaled(2, option.widget), scaled(2, option.widget)
        )
        painter.setPen(QColor("#7663ba"))
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, values[6]
        )
        painter.restore()


THEME = """
QWidget { background: #f8f7f4; color: #252d49;
    font-family: "Microsoft JhengHei UI", "Segoe UI"; font-size: 14px; }
QWidget#contentPage, QWidget#personalizationContent, QScrollArea#personalizationScroll,
QScrollArea#personalizationScroll > QWidget > QWidget { background: transparent; }
QLabel { background: transparent; }
QFrame#primaryNavigation { background: #202840; border: none; }
QFrame#primaryNavigation QPushButton { min-height: 44px; padding: 0; }
QLabel#brandLabel { background-color: #202840; color: #f5f1ff; font-size: 19px;
    font-weight: 800; padding: 8px 4px 22px 4px; }
QLabel#navigationCaption { color: #a8aec8; font-size: 11px; letter-spacing: 2px; }
QLabel#pageTitle { font-size: 30px; font-weight: 800; color: #252d49; }
QLabel#pageDescription, QLabel#backupDetail { color: #7c8092; font-size: 14px; }
QLabel#sectionTitle, QLabel#dialogTitle { font-size: 18px; font-weight: 700; color: #343b5b; }
QPushButton { background: #eeecf3; border: 1px solid #e0dce9; border-radius: 10px;
    padding: 9px 12px; min-height: 22px; color: #4c5270; }
QPushButton:hover:enabled, QPushButton:focus:enabled { background: #e4ddf8; border-color: #a48edc; }
QPushButton:pressed:enabled { background: #d7cbee; }
QPushButton:disabled { color: #aaa7b6; background: #f0eef3; border-color: #e9e6ef; }
QPushButton#primaryButton { background: #7460b4; color: white; border-color: #7460b4;
    font-weight: 700; }
QPushButton#primaryButton:hover:enabled { background: #6551a3; }
QPushButton#primaryButton:disabled { background: #d7d0e7; border-color: #d7d0e7; color: #faf9ff; }
QPushButton#dangerButton { color: #b15361; background: #fff0f1; border-color: #efd0d6; }
QPushButton#dangerButton:disabled { color: #aaa7b6; background: #f0eef3; border-color: #e9e6ef; }
QToolButton#pageHelpButton { background: #fff; color: #7561ae; border: 1px solid #ddd5ed;
    border-radius: 16px; font-weight: 700; min-width: 30px; min-height: 30px; }
QToolButton#pageHelpButton:hover, QToolButton#pageHelpButton:focus { background: #e9e0ff; }
QLabel#metricChip { background: #eae3fa; color: #66539e; padding: 12px 18px;
    border-radius: 12px; font-size: 16px; font-weight: 700; }
QLabel#metricChip[accent="danger"] { background: #f7e6e7; color: #af5b65; }
QFrame#metricCard, QFrame#infoPanel, QFrame#backupResultPanel { background: white;
    border: 1px solid #e6e2ed; border-radius: 14px; }
QLabel#metricTitle { color: #7c8092; }
QLabel#metricValue { font-size: 30px; font-weight: 700; color: #65529d; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDateEdit, QDateTimeEdit,
QListWidget, QTableWidget { background: #fff; border: 1px solid #e2deea;
    border-radius: 9px; padding: 6px; selection-background-color: #e8e0fa;
    selection-color: #343b5b; }
QTableWidget { gridline-color: #eeebf3; alternate-background-color: #faf9fc; }
QHeaderView::section { background: #f1eef7; border: none; padding: 9px; color: #74738a; }
QTableWidget#todayTasksTable { background: transparent; border: none; padding: 0; }
QCalendarWidget { background: white; border: 1px solid #e5dfef; border-radius: 16px; }
QCalendarWidget QWidget#qt_calendar_navigationbar { background: #ede7fa; }
QCalendarWidget QToolButton { color: #54447d; background: transparent; padding: 10px;
    border: none; border-radius: 8px; font-weight: 700; }
QCalendarWidget QToolButton:hover:enabled { background: #ddd1f4; }
QCalendarWidget QToolButton:disabled { color: #b8b5c0; background: #f3f1f5; }
QSpinBox::up-button:off, QSpinBox::down-button:off,
QDateEdit::up-button:off, QDateEdit::down-button:off,
QDateTimeEdit::up-button:off, QDateTimeEdit::down-button:off { background: #eeeef0; }
QSpinBox::up-arrow:off, QSpinBox::down-arrow:off,
QDateEdit::up-arrow:off, QDateEdit::down-arrow:off,
QDateTimeEdit::up-arrow:off, QDateTimeEdit::down-arrow:off { image: none; }
QSpinBox:disabled, QDateEdit:disabled, QDateTimeEdit:disabled,
QComboBox:disabled { color: #aaa7b6; background: #f0eef3; border-color: #e9e6ef; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; }
QScrollBar:vertical { margin: 0px; }
QScrollBar:horizontal { margin: 0px; }
QCalendarWidget QAbstractItemView { background: white; color: #353d5b;
    selection-background-color: #e0d6f7; selection-color: #433166; outline: none; }
QLabel#calendarSelectionFeedback, QLabel#personalizationStatus,
QLabel#gmailStatus, QLabel#gmailAccountStatus { background: #eee8fa; color: #66558e;
    border-radius: 10px; padding: 12px; }
QLabel#emptyState { background: #f2eef8; color: #7b718e; padding: 16px; border-radius: 12px; }
QGroupBox { background: #ffffff; border: 1px solid #e2deea; border-radius: 12px;
    margin-top: 12px; padding: 14px; }
QGroupBox::title { color: #685987; subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QFrame#backupWarning { background: #fcf1db; border-radius: 10px; }
QLabel#validationMessage { color: #b15361; }
QLabel#validationMessage[valid="true"] { color: #56806a; }
QLabel#stickerOverlay { background: transparent; }
QLabel#stickerPreview { background: #fff; border: 1px solid #e2deea; border-radius: 12px; }
QScrollBar:vertical { background: #f1eef5; width: 8px; border: none; }
QScrollBar::handle:vertical { background: #c6bed6; min-height: 26px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def theme_for_scale(scale: float) -> str:
    """Scale pixel-based theme values for an explicit in-app zoom setting."""

    import re

    normalized = min(2.0, max(0.8, float(scale)))

    def replace(match: re.Match[str]) -> str:
        value = int(match.group(1))
        if value == 0:
            return "0px"
        return f"{max(1, round(value * normalized))}px"

    return re.sub(r"(?<![\w.])(\d+)px", replace, THEME)
