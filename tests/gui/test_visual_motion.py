from task_assignment.app import create_application


def test_rapid_navigation_and_disabling_motion_restore_visible_pages(qtbot, tmp_path):
    app, window = create_application([], data_dir=tmp_path)
    qtbot.addWidget(window)
    window.show()
    for page_id in ("calendar", "tasks", "reviews", "settings"):
        window.show_page_by_id(page_id)
        app.processEvents()
    settings = window.page_widgets["settings"]
    settings.motion_combo.setCurrentIndex(settings.motion_combo.findData("off"))
    app.processEvents()
    assert window.services.settings.get("interface_motion") == "off"
    assert window._page_effect.opacity() == 1
    for page in window.page_widgets.values():
        if page.graphicsEffect() is not None:
            assert page.graphicsEffect().opacity() == 1
    window.show_page_by_id("reviews")
    assert window.page_stack.currentWidget() is window.page_widgets["reviews"]
    assert all(button.motion.duration() == 0 for button in window.navigation_buttons.values())
