from task_assignment.ui.pages import ReviewCalendarWidget


def test_workload_bands_darken_and_keep_fixed_boundaries():
    samples = [ReviewCalendarWidget.workload_colors(count)[0] for count in (1, 3, 6, 10)]
    assert all(left.lightnessF() > right.lightnessF()
               for left, right in zip(samples[:-1], samples[1:], strict=True))
    for first, last in ((1, 2), (3, 5), (6, 9), (10, 1000)):
        assert (ReviewCalendarWidget.workload_colors(first)
                == ReviewCalendarWidget.workload_colors(last))


def test_workload_text_has_accessible_contrast():
    def luminance(color):
        rgb = [color.redF(), color.greenF(), color.blueF()]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in rgb]
        return sum(value * weight
                   for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    for count in (1, 3, 6, 10):
        background, foreground = ReviewCalendarWidget.workload_colors(count)
        light, dark = sorted((luminance(background), luminance(foreground)), reverse=True)
        assert (light + 0.05) / (dark + 0.05) >= 4.5
