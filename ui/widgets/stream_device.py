"""SaveSync - lets a media player read a file that is still arriving.

A QIODevice over a core.p2p.transfer.TorrentStream: the player asks for bytes
(from any offset - it seeks to the end of an MP4 for its index, and wherever you
drag the slider), and a read waits until the pieces that cover them have arrived
and been checked against the torrent's own hashes (see TorrentStream). What the
player gets is, byte for byte, what the finished file will contain.

A read that cannot be answered (the download was called off, nothing came for
two minutes, a piece did not match) is an error the player reports, never a
guess. The device is read-only and unbuffered, so the position the player seeks
to is the position that is read.
"""
import logging

from PySide6.QtCore import QIODevice

from core.p2p.transfer import P2pError

logger = logging.getLogger(__name__)


class TorrentIODevice(QIODevice):
    def __init__(self, stream, parent=None):
        super().__init__(parent)
        self._stream = stream
        self._closing = False
        self.open(QIODevice.OpenModeFlag.ReadOnly | QIODevice.OpenModeFlag.Unbuffered)

    def isSequential(self) -> bool:
        return False

    def size(self) -> int:
        return int(self._stream.size)

    def readData(self, maxlen):
        if self._closing:
            return None
        try:
            return self._stream.read(self.pos(), int(maxlen), stop=lambda: self._closing)
        except P2pError as e:
            self.setErrorString(str(e))
            logger.debug("P2P stream: a read could not be answered: %s", e)
            return None
        except Exception:
            logger.debug("P2P stream: a read failed", exc_info=True)
            self.setErrorString("read failed")
            return None

    def writeData(self, data, len_=0):
        return -1

    def stop(self) -> None:
        """No more reads (one that is waiting gives up)."""
        self._closing = True
        try:
            self._stream.release()          # (the file is let go of; the download goes on)
        except Exception:
            pass
        self.close()
