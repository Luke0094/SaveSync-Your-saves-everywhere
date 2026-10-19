"""SaveSync - the one place a picture a friend sent is turned into pixels.

Decoding an image is parsing a file somebody else made, so it is only done when
the person asks to see that picture, and only after the file has passed
core.p2p.attachments.image_problem (a plain image type whose first bytes really
are that type's, and not big) and its header has been read: the format must be
one of a short list and the dimensions within image_limits_ok, with a memory
ceiling on the decoder besides. Only the first frame of an animation is read,
and what comes out is a small thumbnail - the picture is never kept, saved or
handed on.
"""
import logging
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImageReader, QPixmap

from core.p2p import attachments as _a

logger = logging.getLogger(__name__)

_FORMATS = frozenset({b"png", b"jpeg", b"gif", b"webp", b"bmp"})
# What the decoder may allocate, in megabytes (Qt's own default is 256).
_ALLOCATION_LIMIT_MB = 128


def load_preview(path, max_w: int = 360, max_h: int = 260) -> Optional[QPixmap]:
    """A thumbnail of the picture at *path* no bigger than max_w x max_h, or
    None when it is not a picture this app will decode."""
    try:
        if _a.image_problem(path):
            return None
        reader = QImageReader(str(path))
        reader.setDecideFormatFromContent(True)
        reader.setAutoTransform(False)
        try:
            reader.setAllocationLimit(_ALLOCATION_LIMIT_MB)
        except (AttributeError, TypeError):
            pass                                    # an older Qt: its default limit stands
        if bytes(reader.format()).lower() not in _FORMATS:
            return None
        size = reader.size()
        if not size.isValid() or not _a.image_limits_ok(size.width(), size.height()):
            return None
        scale = min(max_w / size.width(), max_h / size.height(), 1.0)
        if scale < 1.0:
            reader.setScaledSize(QSize(max(1, int(size.width() * scale)),
                                       max(1, int(size.height() * scale))))
        image = reader.read()
        if image.isNull():
            return None
        if image.width() > max_w or image.height() > max_h:
            image = image.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        return QPixmap.fromImage(image)
    except Exception:
        logger.debug("P2P: could not preview a picture", exc_info=True)
        return None
