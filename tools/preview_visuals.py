"""Render the visual revision using isolated synthetic tasks."""

import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from task_assignment.app import create_application
from task_assignment.application import TaskDraft
from task_assignment.domain.enums import ScheduleMode
from task_assignment.ui.learning_visuals import CardDeparture, CompletionFeedback, ReviewPath
from task_assignment.ui.task_dialog import TaskDetailsDialog


def main():
    with tempfile.TemporaryDirectory(prefix="task-assignment-preview-") as directory:
        now = datetime(2026, 9, 8, 12)
        app, window = create_application([], data_dir=directory, now_provider=lambda: now)
        for index, name in enumerate(
            ("閱讀：設計日常的細節", "英文單字 · Week 02", "Python 學習筆記", "整理本週的閱讀摘錄")
        ):
            window.services.tasks.create(
                TaskDraft(
                    name=name,
                    description="每天一點累積，慢慢靠近自己的目標。",
                    start_at=now - timedelta(days=7),
                    schedule_mode=ScheduleMode.MANUAL,
                    manual_schedule_times=(
                        now.replace(hour=9) - timedelta(days=1 if index == 0 else 0),
                        now + timedelta(days=index + 1),
                    ),
                )
            )
        window.refresh_all_pages()
        window.resize(1280, 800)
        window.show()
        output = Path(__file__).resolve().parents[1] / "artifacts" / "verification"
        output.mkdir(parents=True, exist_ok=True)
        for page in ("reviews", "calendar"):
            window.show_page_by_id(page)
            if hasattr(window, "_page_motion"):
                window._page_motion.stop()
                window._page_effect.setOpacity(1)
            app.processEvents()
            hero = window.page_widgets["reviews"].hero
            hero.animation.setCurrentTime(hero.animation.duration())
            for button in window.navigation_buttons.values():
                button.motion.setCurrentTime(button.motion.duration())
            app.processEvents()
            path = output / f"2026-09-08-learning-{page}.png"
            window.grab().save(str(path))
            print(path)
        window.show_page_by_id("reviews")
        window._page_motion.stop()
        window._page_effect.setOpacity(1)
        reviews = window.page_widgets["reviews"]
        reviews.table.selectRow(0)
        details = window.services.tasks.details(reviews.items[0].task_id)
        dialog = TaskDetailsDialog(details, window)
        dialog.show()
        app.processEvents()
        for route in dialog.findChildren(ReviewPath):
            route.animation.setCurrentTime(route.animation.duration())
        app.processEvents()
        dialog.grab().save(str(output / "2026-09-08-learning-path.png"))
        dialog.close()
        reviews.complete_button.click()
        departures = reviews.table.viewport().findChildren(CardDeparture)
        for departure in departures:
            departure.animation.pause()
            departure._step(0.5)
        window.grab().save(str(output / "2026-09-08-learning-departure.png"))
        for departure in departures:
            departure.finish()
        app.processEvents()
        for feedback in reviews.table.viewport().findChildren(CompletionFeedback):
            feedback.animation.pause()
            feedback._step(0.45)
        app.processEvents()
        window.grab().save(str(output / "2026-09-08-learning-completion.png"))
        for feedback in reviews.table.viewport().findChildren(CompletionFeedback):
            feedback.hide()
        window.resize(800, 560)
        app.processEvents()
        window.grab().save(str(output / "2026-09-08-learning-compact.png"))
        window.close()
        app.processEvents()
        logging.shutdown()


if __name__ == "__main__":
    main()
