"""Generate splash screen PNG for PyInstaller boot splash and QSplashScreen."""
import sys
from pathlib import Path

# Add project root so we can import constants
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient, QPen
from PySide6.QtCore import Qt, QRectF

from core.constants import APP_NAME, APP_VERSION


def generate_splash(output_path: Path, width: int = 480, height: int = 300):
    app = QApplication.instance() or QApplication(sys.argv)

    pixmap = QPixmap(width, height)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Background gradient
    bg = QLinearGradient(0, 0, 0, height)
    bg.setColorAt(0.0, QColor("#16161a"))
    bg.setColorAt(1.0, QColor("#0e0e11"))
    painter.fillRect(0, 0, width, height, bg)

    # Subtle accent line at top
    accent = QColor("#76b900")
    painter.setPen(QPen(accent, 3))
    painter.drawLine(0, 0, width, 0)

    # App name
    font_title = QFont("Segoe UI", 28, QFont.Bold)
    painter.setFont(font_title)
    painter.setPen(QColor("#e8e8ea"))
    title_rect = QRectF(0, height * 0.28, width, 50)
    painter.drawText(title_rect, Qt.AlignCenter, APP_NAME)

    # Version
    font_ver = QFont("Segoe UI", 11)
    painter.setFont(font_ver)
    painter.setPen(QColor("#76b900"))
    ver_rect = QRectF(0, height * 0.28 + 46, width, 24)
    painter.drawText(ver_rect, Qt.AlignCenter, f"v{APP_VERSION}")

    # Loading text
    font_load = QFont("Segoe UI", 10)
    painter.setFont(font_load)
    painter.setPen(QColor("#4a4a5a"))
    load_rect = QRectF(0, height - 50, width, 30)
    painter.drawText(load_rect, Qt.AlignCenter, "Loading...")

    # Bottom border
    painter.setPen(QPen(QColor("#76b900"), 2))
    painter.drawLine(width // 4, height - 12, width * 3 // 4, height - 12)

    painter.end()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(output_path), "PNG")
    print(f"Splash saved to {output_path}")


if __name__ == "__main__":
    generate_splash(ROOT / "assets" / "splash.png")
