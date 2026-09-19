from task_assignment.app import create_application
from task_assignment.ui.pages import TodayTasksPage


def test_startup_queries_today_once_but_navigation_still_refreshes(qtbot, tmp_path, monkeypatch):
    calls = []
    original = TodayTasksPage.refresh

    def tracked(page):
        calls.append(page)
        return original(page)

    monkeypatch.setattr(TodayTasksPage, "refresh", tracked)
    _, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    assert len(calls) == 1
    window.show_page_by_id("tasks")
    window.show_page_by_id("reviews")
    assert len(calls) == 2
    log = (tmp_path / "logs" / "task_assignment.log").read_text(encoding="utf-8")
    assert "Startup phases ms:" in log
    for stage in ("imports=", "storage=", "statuses=", "qt=", "shell=", "total="):
        assert stage in log
