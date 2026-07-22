"""Generate animated splash GIF with full-S particle field and smooth arrow wrap-around."""
import sys
from pathlib import Path
import io
import math

# Add project root so we can import constants
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QLinearGradient, QPen
from PySide6.QtCore import Qt, QRectF, QBuffer, QIODevice
from PIL import Image

from core.constants import APP_NAME, APP_VERSION


def draw_qt_frame(width: int, height: int, frame_index: int, num_frames: int, border_progress: int, dots: int) -> QPixmap:
    """Disegna il frame con effetto dissolvenza sul rientro e particelle orbitali attorno alla S."""
    pixmap = QPixmap(width, height)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)

    # 1. Background gradient
    bg = QLinearGradient(0, 0, 0, height)
    bg.setColorAt(0.0, QColor("#16161a"))
    bg.setColorAt(1.0, QColor("#0e0e11"))
    painter.fillRect(0, 0, width, height, bg)

    # 2. Subtle accent line at top
    accent = QColor("#76b900")
    painter.setPen(QPen(accent, 3))
    painter.drawLine(0, 0, width, 0)

    # 3. App name
    font_title = QFont("Segoe UI", 28, QFont.Bold)
    painter.setFont(font_title)
    painter.setPen(QColor("#e8e8ea"))
    title_rect = QRectF(0, height * 0.28, width, 50)
    painter.drawText(title_rect, Qt.AlignCenter, APP_NAME)

    # 4. Version
    font_ver = QFont("Segoe UI", 11)
    painter.setFont(font_ver)
    painter.setPen(QColor("#76b900"))
    ver_rect = QRectF(0, height * 0.28 + 46, width, 24)
    painter.drawText(ver_rect, Qt.AlignCenter, f"v{APP_VERSION}")

    # ================= AREA DI SCANSIONE E PARTICELLE "S" =================
    s_center_y = height * 0.64
    font_s = QFont("Segoe UI", 52, QFont.Bold)
    painter.setFont(font_s)
    painter.setPen(QColor("#76b900"))
    s_rect = QRectF(0, s_center_y - 30, width, 60)
    painter.drawText(s_rect, Qt.AlignCenter, "S")

    # Confini verticali della S
    y_min = s_center_y - 38  
    y_max = s_center_y + 48  
    scan_range = y_max - y_min

    center_x = width // 2
    line_len = width * 0.18
    line_width = max(1, width // 80)

    # ================= EFFETTO NEBULA PARTICELLE (ATTORNO ALLA S) =================
    # Genera un campo di micro-bagliori che fluttuano in modo permanente intorno alla lettera
    num_particles = 24
    for p in range(num_particles):
        # Angolo di oscillazione unico per particella
        p_angle = (frame_index / num_frames) * 2 * math.pi + (p * 13.7)
        
        # Distribuzione ad anello/ellisse attorno alla S
        p_x = center_x + int(math.cos(p_angle) * (line_len * 0.6) + math.sin(p * 5.3) * 8)
        p_y = s_center_y + int(math.sin(p_angle * 1.5) * 32 + math.cos(p * 2.9) * 5)
        
        # Calcolo opacità: scintillio naturale + boost di luminosità se le frecce sono vicine
        base_alpha = int(40 + 120 * abs(math.sin(frame_index * 0.5 + p * 1.1)))
        
        p_color = QColor("#76b900") if p % 3 == 0 else QColor("#e8e8ea")
        p_color.setAlpha(base_alpha)
        
        # Disegna la particella come micro-punto energetico (1x1 o 2x2 alternati)
        painter.setPen(QPen(p_color, 1 if p % 2 == 0 else 2))
        painter.drawPoint(p_x, p_y)

    # ================= LOGICA SCORRIMENTO + FADE DI RIENTRO OMAGENEO =================
    # Avanzamento a 2 cicli completi
    linear_progress = ((frame_index / num_frames) * 2 * scan_range) % scan_range
    arrow1_y = y_max - linear_progress
    arrow2_y = arrow1_y + 14

    # Calcolo del fattore di Fade (Trasparenza progressiva vicino ai bordi per evitare scatti)
    # Più la freccia è vicina a y_min (cima) o y_max (base), più l'alpha si azzera dolcemente
    edge_threshold = 12.0  # Zona di transizione in pixel
    
    # Controllo Freccia 1
    dist_to_top1 = arrow1_y - y_min
    dist_to_bot1 = y_max - arrow1_y
    fade_factor1 = min(1.0, dist_to_top1 / edge_threshold, dist_to_bot1 / edge_threshold)
    fade_factor1 = max(0.0, fade_factor1)

    # Controllo Freccia 2
    dist_to_top2 = arrow2_y - y_min
    dist_to_bot2 = y_max - arrow2_y
    fade_factor2 = min(1.0, dist_to_top2 / edge_threshold, dist_to_bot2 / edge_threshold)
    fade_factor2 = max(0.0, fade_factor2)

    # Shimmer di base a cui applichiamo il moltiplicatore di fade del rientro
    alpha_base1 = int(180 + 75 * math.sin(frame_index * 0.9) * math.cos(frame_index * 0.4))
    alpha_base2 = int(180 + 75 * math.cos(frame_index * 0.8) * math.sin(frame_index * 0.5))
    
    final_alpha1 = int(alpha_base1 * fade_factor1)
    final_alpha2 = int(alpha_base2 * fade_factor2)

    # --- Disegno Freccia destra (→) ---
    if final_alpha1 > 5:
        painter.setPen(QPen(QColor(232, 232, 234, final_alpha1 // 4), line_width + 2)) # Glow
        painter.drawLine(center_x - line_len//2, arrow1_y, center_x + line_len//2, arrow1_y)
        painter.setPen(QPen(QColor(232, 232, 234, final_alpha1), line_width))          # Core
        painter.drawLine(center_x - line_len//2, arrow1_y, center_x + line_len//2, arrow1_y)
        painter.drawLine(center_x + line_len//2, arrow1_y, center_x + line_len//2 - line_width*3, arrow1_y - line_width*2)

    # --- Disegno Freccia sinistra (←) ---
    if final_alpha2 > 5:
        painter.setPen(QPen(QColor(232, 232, 234, final_alpha2 // 4), line_width + 2)) # Glow
        painter.drawLine(center_x - line_len//2, arrow2_y, center_x + line_len//2, arrow2_y)
        painter.setPen(QPen(QColor(232, 232, 234, final_alpha2), line_width))          # Core
        painter.drawLine(center_x - line_len//2, arrow2_y, center_x + line_len//2, arrow2_y)
        painter.drawLine(center_x - line_len//2, arrow2_y, center_x - line_len//2 + line_width*3, arrow2_y - line_width*2)

    # --- Colonne energetiche verticali del Transporter (Legate alle frecce visibili) ---
    avg_alpha = (final_alpha1 + final_alpha2) // 2
    if avg_alpha > 10:
        for k in range(12):
            x_factor = (math.sin(frame_index * 0.7 + k * 2.3) * 0.5 + 0.5)
            sparkle_x = int((center_x - line_len // 2) + x_factor * line_len)
            
            # Unisce verticalmente le due frecce solo se sono all'interno dello schermo
            y_t = max(y_min, min(arrow1_y, arrow2_y))
            y_b = min(y_max, max(arrow1_y, arrow2_y))
            
            if (y_b - y_t) > 2:
                s_alpha = int((30 + 180 * abs(math.sin(frame_index * 0.8 + k * 1.1))) * ((fade_factor1 + fade_factor2)/2))
                s_color = QColor("#e8e8ea") if k % 3 == 0 else QColor("#76b900")
                s_color.setAlpha(max(0, min(255, s_alpha)))
                painter.setPen(QPen(s_color, 1))
                painter.drawLine(sparkle_x, y_t, sparkle_x, y_b)

    # ===================================================================

    # 7. Loading Text
    font_load = QFont("Segoe UI", 10)
    painter.setFont(font_load)
    painter.setPen(QColor("#4a4a5a"))
    
    metrics = painter.fontMetrics()
    total_w = metrics.horizontalAdvance("Loading...")
    start_x = (width - total_w) // 2
    load_rect = QRectF(start_x, height - 50, total_w, 30)
    
    dots_str = "." * dots
    painter.drawText(load_rect, Qt.AlignLeft | Qt.AlignVCenter, f"Loading{dots_str}")

    # 8. Bottom Border Animato
    half_width_max = width // 4
    current_half_width = int(half_width_max * (border_progress / 100))
    start_xb = width // 2 - current_half_width
    end_xb = width // 2 + current_half_width
    
    painter.setPen(QPen(QColor("#76b900"), 2))
    painter.drawLine(start_xb, height - 12, end_xb, height - 12)

    painter.end()
    return pixmap


def generate_animated_gif(output_path: Path, width: int = 480, height: int = 300, duration: int = 5000):
    """Genera la GIF animata definitiva tramite pipeline Qt/Pillow."""
    app = QApplication.instance() or QApplication(sys.argv)
    
    frames = []
    num_frames = 55  # Aumentato leggermente il campionamento per catturare l'effetto pulviscolo senza artefatti
    frame_duration = duration // num_frames
    
    print(f"Rendering di {num_frames} frame (Campo particelle S totale + Fade Wrap-Around)...")
    
    for i in range(num_frames):
        border_progress = int((i / (num_frames - 1)) * 100)
        dots = ((i // 12) % 4)
        
        qt_pixmap = draw_qt_frame(width, height, i, num_frames, border_progress, dots)
        
        q_buffer = QBuffer()
        q_buffer.open(QIODevice.WriteOnly)
        qt_pixmap.save(q_buffer, "PNG")
        
        pil_img = Image.open(io.BytesIO(bytes(q_buffer.data()))).convert("RGB")
        pil_img_p = pil_img.convert("P", palette=Image.Palette.ADAPTIVE, colors=256)
        frames.append(pil_img_p)
        
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output_path,
        save_all=True,
        append_images=frames[1:],
        duration=frame_duration,
        loop=0,
        optimize=True
    )
    print(f"\n✅ GIF cinematografica completata con successo in {output_path}!")


if __name__ == "__main__":
    assets_dir = ROOT / "assets"
    generate_animated_gif(assets_dir / "splash_animated.gif", duration=5000)