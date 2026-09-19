"""Render the repository SVG into a Windows executable icon."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    source = project_root / "resources" / "icons" / "task_assignment.svg"
    target = source.with_suffix(".ico")
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise RuntimeError(f"無法讀取 SVG：{source}")
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(QColor(Qt.GlobalColor.transparent))
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    if not image.save(str(target), "ICO"):
        raise RuntimeError(f"無法寫入 ICO：{target}")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
