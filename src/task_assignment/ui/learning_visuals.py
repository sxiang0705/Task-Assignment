"""Learning-focused visuals; animation never owns or delays data mutations."""

import math
from datetime import date

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget


class CardDeparture(QWidget):
    """Short-lived visual snapshot; the live model is already committed and refreshed."""

    @staticmethod
    def cancel(table):
        for overlay in table.viewport().findChildren(CardDeparture):
            overlay.finish()

    def __init__(self, table, before, row_rect, mode):
        super().__init__(table.viewport())
        self.table = table
        self.before = before
        self.after = table.viewport().grab()
        self.row_rect = QRectF(row_rect)
        self.mode = mode
        self.phase = 0.0
        self.setGeometry(table.viewport().rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(280 if mode == "full" else 120)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self.finish)
        self._watched = (table, table.viewport(), table.verticalScrollBar())
        for widget in self._watched:
            widget.installEventFilter(self)
        self.show()
        self.animation.start()

    def _step(self, value):
        self.phase = value
        self.update()

    def finish(self):
        self.animation.stop()
        for widget in self._watched:
            widget.removeEventFilter(self)
        self.hide()
        self.deleteLater()

    def eventFilter(self, watched, event):
        # Never allow a click on a moving snapshot to select a different live row.
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            self.finish()
            return True
        if event.type() in (
            QEvent.Type.Wheel, QEvent.Type.KeyPress, QEvent.Type.Resize,
            QEvent.Type.Hide, QEvent.Type.MouseMove,
        ):
            self.finish()
        return False

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(QPointF(0, 0), self.after)
        if self.mode != "full":
            p.setOpacity(1 - self.phase)
            p.drawPixmap(QPointF(0, 0), self.before)
            return
        top = max(0.0, self.row_rect.top())
        offset = self.row_rect.height() * (1 - self.phase)
        p.setClipRect(QRectF(0, top, self.width(), self.height() - top))
        p.fillRect(self.rect(), QColor("#f8f7f4"))
        p.save()
        p.setClipRect(QRectF(0, top + offset, self.width(), self.height()))
        p.drawPixmap(QPointF(0, offset), self.after)
        p.restore()
        p.setOpacity(1 - self.phase)
        p.setClipRect(QRectF(0, top, self.width(), offset))
        p.drawPixmap(QPointF(24 * self.phase, 0), self.before)


class RevealEffect(QGraphicsOpacityEffect):
    distance = 10
    horizontal = False

    def draw(self, painter):
        painter.save()
        offset = (1 - self.opacity()) * self.distance
        painter.translate(offset if self.horizontal else 0, 0 if self.horizontal else offset)
        super().draw(painter)
        painter.restore()


def reveal(widget, mode, direction=0):
    if not widget.isVisible():
        return
    previous = getattr(widget, "_reveal_motion", None)
    if previous is not None:
        previous.stop()
        previous.deleteLater()
    effect = widget.graphicsEffect()
    if not isinstance(effect, RevealEffect):
        effect = RevealEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.distance = (direction * 30 if direction else 10) if mode == "full" else 0
    effect.horizontal = bool(direction)
    effect.setOpacity(1)
    if mode == "off":
        widget._reveal_motion = None
        return
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(280 if mode == "full" else 120)
    animation.setStartValue(0.05)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    widget._reveal_motion = animation
    animation.start()


def motion_mode(widget):
    current = widget
    while current is not None:
        services = getattr(current, "services", None)
        if services is not None:
            return services.settings.get("interface_motion", "full")
        current = current.parentWidget()
    return "full"


class LearningHero(QWidget):
    """Compact responsive summary using real due counts, without invented progress."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(144)
        self.today = 0
        self.overdue = 0
        self.display_count = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._step)
        self.setAccessibleName("今日複習摘要")

    def _step(self, value):
        self.display_count = value
        self.update()

    def set_counts(self, today, overdue):
        self.today, self.overdue = today, overdue
        self.setAccessibleDescription(f"今日 {today} 筆，逾期 {overdue} 筆")
        self.animation.stop()
        self.animation.setStartValue(self.display_count)
        self.animation.setEndValue(float(today))
        self.animation.setDuration(450 if motion_mode(self) == "full" else 0)
        self.animation.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0, 0, -1, -1)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor("#282e50"))
        gradient.setColorAt(0.6, QColor("#47406e"))
        gradient.setColorAt(1, QColor("#73609b"))
        p.setBrush(gradient)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 20, 20)
        glow = QRadialGradient(QPointF(self.width() * 0.78, 5), 210)
        glow.setColorAt(0, QColor(184, 146, 255, 90))
        glow.setColorAt(1, QColor(184, 146, 255, 0))
        p.setBrush(glow)
        p.drawRoundedRect(rect, 20, 20)
        p.setPen(QColor("#c5bce6"))
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        p.setFont(font)
        p.drawText(24, 30, "YOUR LEARNING JOURNEY")
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
        font.setPixelSize(23)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        p.drawText(
            24, 66, "讓記憶，一步步留下來。" if self.width() > 540 else "每一次複習，都是進步。"
        )
        font.setPixelSize(12)
        font.setBold(False)
        p.setFont(font)
        p.setPen(QColor("#ded7ec"))
        p.drawText(24, 98, f"今日 {self.today} 筆  ·  逾期 {self.overdue} 筆")
        p.drawText(
            24,
            122,
            "選一張卡片，開始今天的複習。"
            if self.today + self.overdue
            else "目前沒有待處理複習，享受一點留白。",
        )
        if self.width() > 540:
            x = self.width() - 110
            p.setPen(QPen(QColor("#b5a2e4"), 1.5))
            p.setBrush(QColor(218, 205, 255, 18))
            p.drawEllipse(QPointF(x, 70), 48, 48)
            font.setPixelSize(32)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor("#ffffff"))
            p.drawText(
                QRectF(x - 45, 39, 90, 46),
                Qt.AlignmentFlag.AlignCenter,
                str(round(self.display_count)),
            )
            font.setPixelSize(11)
            font.setBold(False)
            p.setFont(font)
            p.drawText(QRectF(x - 45, 84, 90, 20), Qt.AlignmentFlag.AlignCenter, "今日待複習")


class CompletionFeedback(QWidget):
    """Non-interactive celebration shown only after the service has succeeded."""

    def __init__(self, parent, message, mode, progress=None):
        super().__init__(parent)
        self.message = message
        self.mode = mode
        self.progress = progress
        self.phase = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(parent.rect())
        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setDuration(850 if mode == "full" else 450)
        self.animation.valueChanged.connect(self._step)
        self.animation.finished.connect(self.deleteLater)
        self.show()
        self.raise_()
        if mode == "off":
            self.phase = 0.4
            QTimer.singleShot(1000, self.deleteLater)
        else:
            self.animation.start()

    def _step(self, value):
        self.phase = value
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha = min(1, self.phase * 6, (1 - self.phase) * 4) if self.mode != "off" else 1
        p.setOpacity(alpha)
        center = QPointF(self.width() / 2, min(self.height() / 2, 150))
        box = QRectF(center.x() - 155, center.y() - 60, 310, 120)
        p.setBrush(QColor("#332e51"))
        p.setPen(QPen(QColor("#b7a0ec"), 1))
        p.drawRoundedRect(box, 22, 22)
        p.setPen(QPen(QColor("#bdebd9"), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(center + QPointF(-13, -20), center + QPointF(-3, -9))
        p.drawLine(center + QPointF(-3, -9), center + QPointF(18, -32))
        p.setPen(QColor("#ffffff"))
        p.drawText(box.adjusted(12, 68, -12, -8), Qt.AlignmentFlag.AlignCenter, self.message)
        if self.progress is not None:
            start, end = self.progress
            ratio = min(1.0, self.phase * 2.5) if self.mode == "full" else 1.0
            value = start + (end - start) * ratio
            p.setPen(QPen(QColor("#c4b0f6"), 3))
            p.drawLine(
                QPointF(box.left() + 30, box.bottom() - 12),
                QPointF(box.left() + 30 + 190 * value / 100, box.bottom() - 12),
            )
            p.setPen(QColor("#ddd4f5"))
            p.drawText(
                box.adjusted(0, 0, -15, -2),
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight,
                f"{value:.0f}%",
            )
        if self.mode == "full":
            for index in range(10):
                angle = index * math.tau / 10
                radius = 35 + self.phase * 95
                point = center + QPointF(math.cos(angle) * radius, math.sin(angle) * radius)
                p.setBrush(QColor("#d3baff" if index % 2 else "#c1dfff"))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(point, 2.5, 2.5)


class ReviewPath(QWidget):
    """Chronological schedule nodes with exact dates and explicit status labels."""

    def __init__(self, schedules, today=None, parent=None):
        super().__init__(parent)
        self.schedules = sorted(schedules, key=lambda item: item.scheduled_at)
        self.today = today or date.today()
        self.setMinimumSize(max(340, len(self.schedules) * 100 + 30), 122)
        self.setMouseTracking(True)
        self.setAccessibleName("複習路徑；各節點依實際預定日期排序")
        self.progress = 1.0
        self.animation = QVariantAnimation(self)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setDuration(550)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.valueChanged.connect(self._step)

    def _step(self, value):
        self.progress = value
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        if motion_mode(self) == "full":
            self.animation.start()

    def hideEvent(self, event):
        self.animation.stop()
        self.progress = 1.0
        super().hideEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#776991"))
        p.drawText(16, 23, "REVIEW PATH  /  複習路徑")
        p.setClipRect(QRectF(0, 30, self.width() * self.progress, self.height()))
        for index, schedule in enumerate(self.schedules):
            x = 50 + index * 100
            status = schedule.status.value
            label, color = (
                ("已完成", "#6b9e8b")
                if status == "completed"
                else (
                    ("已略過", "#9291a0")
                    if status == "skipped"
                    else ("逾期", "#bd6670")
                    if schedule.scheduled_at.date() < self.today
                    else ("今日", "#8b6ac9")
                    if schedule.scheduled_at.date() == self.today
                    else ("未來", "#aca3bf")
                )
            )
            if index:
                p.setPen(QPen(QColor("#dcd3ed"), 2))
                p.drawLine(x - 90, 53, x - 10, 53)
            p.setPen(QPen(QColor(color), 2))
            p.setBrush(QColor(color) if status != "pending" else QColor("#faf7ff"))
            p.drawEllipse(QPointF(x, 53), 8, 8)
            p.setPen(QColor("#51465f"))
            p.drawText(
                QRectF(x - 46, 72, 92, 18),
                Qt.AlignmentFlag.AlignCenter,
                schedule.scheduled_at.strftime("%m/%d"),
            )
            p.drawText(
                QRectF(x - 46, 94, 92, 18),
                Qt.AlignmentFlag.AlignCenter,
                f"第 {schedule.sequence} 次 · {label}",
            )

    def mouseMoveEvent(self, event):
        index = round((event.position().x() - 50) / 100)
        if 0 <= index < len(self.schedules):
            item = self.schedules[index]
            self.setToolTip(f"第 {item.sequence} 次複習\n{item.scheduled_at:%Y/%m/%d %H:%M}")
        else:
            self.setToolTip("")
        super().mouseMoveEvent(event)
