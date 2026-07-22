"""Generate SaveSync app icon (.ico) with multiple sizes."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient, QPen, QImage
from PySide6.QtCore import Qt, QRectF
from PIL import Image
import io


def render_icon(size: int) -> QImage:
    """Render a single icon at the given size."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    # Rounded-rect background
    margin = max(1, size // 32)
    radius = size * 0.18
    bg = QLinearGradient(0, 0, 0, size)
    bg.setColorAt(0.0, QColor("#1e1e24"))
    bg.setColorAt(1.0, QColor("#111114"))
    p.setPen(Qt.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(margin, margin, size - margin * 2, size - margin * 2, radius, radius)

    # Accent border
    pen = QPen(QColor("#76b900"), max(1, size // 32))
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(margin, margin, size - margin * 2, size - margin * 2, radius, radius)

    # "S" letter with sync arrows feel
    font_size = int(size * 0.52)
    font = QFont("Segoe UI", font_size, QFont.Bold)
    p.setFont(font)
    p.setPen(QColor("#76b900"))
    text_rect = QRectF(0, -size * 0.02, size, size)
    p.drawText(text_rect, Qt.AlignCenter, "S")

    # Small sync arrows (two small curved strokes under the S)
    if size >= 48:
        arrow_pen = QPen(QColor("#e8e8ea"), max(1, size // 40))
        arrow_pen.setCapStyle(Qt.RoundCap)
        p.setPen(arrow_pen)
        cx, cy = size * 0.5, size * 0.78
        span = size * 0.18
        # Right arrow →
        p.drawLine(int(cx - span), int(cy), int(cx + span), int(cy))
        p.drawLine(int(cx + span), int(cy), int(cx + span - size * 0.06), int(cy - size * 0.05))
        # Left arrow ← (below)
        cy2 = cy + size * 0.08
        p.drawLine(int(cx + span), int(cy2), int(cx - span), int(cy2))
        p.drawLine(int(cx - span), int(cy2), int(cx - span + size * 0.06), int(cy2 - size * 0.05))

    p.end()
    return pm.toImage()


def qimage_to_pil(qimg: QImage) -> Image.Image:
    """Convert QImage (ARGB32) to PIL Image (RGBA)."""
    qimg = qimg.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = qimg.width(), qimg.height()
    # Access raw pixel data
    ptr = qimg.constBits()
    raw = bytes(ptr)
    return Image.frombytes("RGBA", (width, height), raw)


def generate_ico(output_path: Path):
    app = QApplication.instance() or QApplication(sys.argv)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    pil_images = []
    for s in sizes:
        qimg = render_icon(s)
        pil_img = qimage_to_pil(qimg)
        pil_images.append(pil_img)

    # Also save the 256px version as PNG for other uses
    pil_images[-1].save(str(output_path.parent / "icon.png"), "PNG")

    # Save as .ico (first image is the "main", rest are appended)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pil_images[-1].save(
        str(output_path),
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=pil_images[:-1],
    )
    print(f"Icon saved to {output_path}")
    print(f"PNG saved to {output_path.parent / 'icon.png'}")


if __name__ == "__main__":
    generate_ico(ROOT / "assets" / "icon.ico")
