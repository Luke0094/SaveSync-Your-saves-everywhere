"""SaveSync - the one place a video a friend sent is played.

A media library parsing a file somebody else made is the riskiest thing in the
chat, so this is as small as it can be and holds it to a short list of rules:

- it only exists when the person pressed "Play video" on a saved attachment (or
  "Watch while downloading" on one that is still arriving) - nothing is played,
  probed or thumbnailed by itself, and never from a notification or a preview;
- the file must be a plain video container with the signature of its type
  (core.p2p.attachments.video_problem), not a link, not a playlist;
- the player is given the file's BYTES (a read-only QFile as its device, or, for
  one still arriving, a device that answers only with pieces that have arrived
  and match the torrent's hashes - see ui.widgets.stream_device), not a path or
  an address: it has nothing to open but that one file, no other file, no
  network;
- one at a time, no full screen, nothing that opens another program; it is
  closed by the person, when the chat page is left, or when it fails;
- if the file has not loaded in LOAD_SECONDS (STREAM_LOAD_SECONDS while it is
  still arriving), or the library reports an error (for instance a codec this PC
  does not have), it is dropped and says so.

It still runs inside SaveSync, and a flaw in a decoder is a flaw in SaveSync:
play videos from people you trust. (Settings can turn all of this off.)
"""
import logging
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QTimer, QUrl, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from core.p2p import attachments as _a
from i18n import t

logger = logging.getLogger(__name__)

LOAD_SECONDS = 20
STREAM_LOAD_SECONDS = 120          # (a slow download has to bring the start of the file first)
SCREEN_W, SCREEN_H = 400, 225
_VOLUME = 0.5


def available() -> bool:
    """Is Qt's multimedia part installed (some builds leave it out)?"""
    try:
        from PySide6 import QtMultimedia, QtMultimediaWidgets  # noqa: F401
        return True
    except Exception:
        return False


def _clock(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


def _screen_class():
    from PySide6.QtMultimediaWidgets import QVideoWidget

    class _Screen(QVideoWidget):
        """The picture. A double click would take it full screen: it does not."""

        def mouseDoubleClickEvent(self, event):
            event.ignore()

        def setFullScreen(self, full):          # (never)
            return

    return _Screen


class SafeVideoPlayer(QWidget):
    closed = Signal()

    def __init__(self, path, parent=None, *, stream=None):
        """Play the video file at *path* - or, with *stream* (a
        core.p2p.transfer.TorrentStream whose start has arrived), the one that is
        still being downloaded. ValueError for what is not a video of a kind the
        chat plays."""
        super().__init__(parent)
        self._stream = stream
        if stream is None:
            if _a.video_problem(path):
                raise ValueError("not a video this app will play")
            self._path = Path(path)
        else:
            self._path = Path(stream.name)
            if not _a.video_name_ok(stream.name) or not stream.ready():
                raise ValueError("not a video this app will play (yet)")
            try:
                head = stream.read(0, 400)
            except Exception as e:
                raise ValueError("the start of the video cannot be read") from e
            if len(head) < 16 or not _a.video_signature_ok(stream.name, head):
                raise ValueError("not a video this app will play")
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        self._QMediaPlayer = QMediaPlayer
        self._failed = False
        self._loaded = False
        if stream is None:
            self._file = QFile(str(self._path), self)
            if not self._file.open(QIODevice.OpenModeFlag.ReadOnly):
                raise OSError("cannot read the file")
        else:
            from ui.widgets.stream_device import TorrentIODevice
            self._file = TorrentIODevice(stream, self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)
        self.screen = _screen_class()()
        self.screen.setFixedSize(SCREEN_W, SCREEN_H)
        self.screen.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.screen.setStyleSheet("background: black;")
        lay.addWidget(self.screen)
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setTextFormat(Qt.TextFormat.PlainText)
        self._note.setVisible(False)
        lay.addWidget(self._note)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.play_btn = QPushButton("⏸")
        self.play_btn.setFixedWidth(34)
        self.play_btn.clicked.connect(self._toggle)
        row.addWidget(self.play_btn)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(lambda ms: self._player.setPosition(int(ms)))
        row.addWidget(self.slider, 1)
        self.time_lbl = QLabel("0:00 / 0:00")
        row.addWidget(self.time_lbl)
        self.mute_btn = QPushButton("🔊")
        self.mute_btn.setFixedWidth(34)
        self.mute_btn.clicked.connect(self._mute)
        row.addWidget(self.mute_btn)
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedWidth(34)
        self.close_btn.setToolTip(t("p2p.friends_video_close"))
        self.close_btn.clicked.connect(self.close_player)
        row.addWidget(self.close_btn)
        lay.addLayout(row)

        self._audio = QAudioOutput(self)
        self._audio.setVolume(_VOLUME)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self.screen)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._player.errorOccurred.connect(self._on_error)
        self.screen.videoSink().videoFrameChanged.connect(lambda _frame: self._got_something())
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.setInterval((STREAM_LOAD_SECONDS if stream is not None else LOAD_SECONDS) * 1000)
        self._watchdog.timeout.connect(lambda: None if self._loaded else self._fail(t("p2p.friends_video_timeout")))
        self._watchdog.start()
        # The player is handed the bytes of this one file, nothing more - and
        # not even where it is: the name given with them is a made-up one (the
        # extension is only a hint for the format), so nothing a container
        # says about "the file next to me" has a place to point to.
        ext = self._path.suffix.lower().lstrip(".") or "video"
        self._player.setSourceDevice(self._file, QUrl(f"file:///savesync-chat-video/clip.{ext}"))
        self._player.play()

    # ── controls ─────────────────────────────────────────────────────────

    def _toggle(self):
        if self._failed:
            return
        if self._player.playbackState() == self._QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _mute(self):
        muted = not self._audio.isMuted()
        self._audio.setMuted(muted)
        self.mute_btn.setText("🔇" if muted else "🔊")

    def _got_something(self):
        """A picture or a sound has really come out: it is a video after all."""
        if not self._loaded:
            self._loaded = True
            self._watchdog.stop()

    def _check_streams(self):
        """The library says it is loaded, yet found neither picture nor sound
        (what a file made of a signature and rubbish comes to): not a video."""
        if (not self._failed and not self._loaded and not self._player.hasVideo()
                and not self._player.hasAudio()):
            self._fail(t("p2p.friends_video_invalid"))

    def _on_position(self, ms: int):
        if ms > 0 and (self._player.hasVideo() or self._player.hasAudio()):
            self._got_something()            # (a clock running over nothing proves nothing)
        if not self.slider.isSliderDown():
            self.slider.setValue(int(ms))
        self.time_lbl.setText(f"{_clock(ms)} / {_clock(self._player.duration())}")

    def _on_duration(self, ms: int):
        self.slider.setRange(0, int(ms))
        self.time_lbl.setText(f"{_clock(self._player.position())} / {_clock(ms)}")

    def _on_state(self, state):
        playing = state == self._QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("⏸" if playing else "▶")

    def _on_status(self, status):
        S = self._QMediaPlayer.MediaStatus
        if status == S.EndOfMedia:
            self._got_something()
        elif status in (S.LoadedMedia, S.BufferedMedia, S.BufferingMedia):
            # (not yet proof of anything: wait for a frame, or for nothing at all)
            QTimer.singleShot(1500, self, self._check_streams)
        elif status == S.InvalidMedia:
            self._fail(t("p2p.friends_video_invalid"))
        if self._stream is not None and not self._failed:
            # A video still arriving can run ahead of its download: say so while it waits.
            waiting = status in (S.StalledMedia, S.BufferingMedia) and self._loaded
            self._note.setText(t("p2p.friends_video_buffering") if waiting else "")
            self._note.setVisible(waiting)

    def _on_error(self, _error, text=""):
        self._fail(t("p2p.friends_video_invalid"))
        logger.debug("P2P friends: a video would not play: %s", text)

    def _fail(self, why: str):
        if self._failed:
            return
        self._failed = True
        self._watchdog.stop()
        try:
            self._player.stop()
        except Exception:
            pass
        self._note.setText(why)
        self._note.setVisible(True)
        self.play_btn.setEnabled(False)
        self.slider.setEnabled(False)

    # ── ending ───────────────────────────────────────────────────────────

    def close_player(self):
        """Stop, let go of the file, and go."""
        try:
            self._watchdog.stop()
            self._player.stop()
            self._player.setVideoOutput(None)
            self._player.setSource(QUrl())
            if self._stream is not None:
                self._file.stop()               # (the download is not touched)
            else:
                self._file.close()
        except Exception:
            logger.debug("P2P friends: closing a player failed", exc_info=True)
        self.hide()
        self.closed.emit()
        self.deleteLater()
