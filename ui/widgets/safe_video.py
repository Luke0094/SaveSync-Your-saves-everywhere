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
- one at a time, nothing that opens another program; it is closed by the
  person, when the chat page is left, or when it fails. It goes full screen
  only when the person presses the button for it: the same player, the same
  bytes, drawn in a window of its own (Esc, a double click or the button ends
  it, and so does anything that closes the player);
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
    QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from core.p2p import attachments as _a
from i18n import t
from ui.widgets.media_fullscreen import IdleChrome

logger = logging.getLogger(__name__)

LOAD_SECONDS = 20
STREAM_LOAD_SECONDS = 120          # (a slow download has to bring the start of the file first)
SCREEN_W, SCREEN_H = 400, 225
BUTTON_W, BUTTON_H = 34, 28
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

        def setFullScreen(self, full):          # (never: full screen is the player's own, see _FullscreenWindow)
            return

    return _Screen


class _FullscreenWindow(QWidget):
    """The player's picture and a row of controls on a black window that covers the screen. It
    owns nothing: the player's own QMediaPlayer is pointed at its screen for as long as it is up,
    and the controls call the player's own. ``exit_requested`` is asked for by Esc, a double click
    and the exit button; the player does the leaving."""
    exit_requested = Signal()

    def __init__(self, player, parent):
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self._p = player
        self.setObjectName("safe_video_fullscreen")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The picture fills the window and the controls float over its bottom edge (never in a
        # layout: hiding them must not resize the picture) - see IdleChrome.
        self.screen = _screen_class()()
        self.screen.setParent(self)
        self.screen.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.screen.setStyleSheet("background: black;")
        bar = QWidget(self)
        bar.setObjectName("safe_video_fs_bar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._bar = bar
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 10)
        row.setSpacing(8)
        self.play_btn = QPushButton("⏸")
        self.play_btn.clicked.connect(player._toggle)
        row.addWidget(self.play_btn)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(lambda ms: player._player.setPosition(int(ms)))
        row.addWidget(self.slider, 1)
        self.time_lbl = QLabel("0:00 / 0:00")
        row.addWidget(self.time_lbl)
        self.mute_btn = QPushButton("🔊")
        self.mute_btn.clicked.connect(player._mute)
        row.addWidget(self.mute_btn)
        self.exit_btn = QPushButton("⤡")
        self.exit_btn.setToolTip(t("p2p.friends_fullscreen_exit"))
        self.exit_btn.clicked.connect(self.exit_requested)
        row.addWidget(self.exit_btn)
        for btn in (self.play_btn, self.mute_btn, self.exit_btn):
            btn.setFixedSize(BUTTON_W, BUTTON_H)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("#safe_video_fullscreen{background:black;}"
                           "#safe_video_fs_bar{background:rgba(0,0,0,0.6);}" + player.control_style())
        self.idle = IdleChrome(self, chrome=[bar],
                               controls=[self.play_btn, self.slider, self.mute_btn, self.exit_btn],
                               watch=[self.screen, bar])
        self._place()

    def _place(self):
        w, h = self.width(), self.height()
        self.screen.setGeometry(0, 0, w, h)
        bh = self._bar.sizeHint().height()
        self._bar.setGeometry(0, max(0, h - bh), w, bh)
        self._bar.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def sync(self):
        """Take the player's position, length, state and mute as they are now."""
        p = self._p
        if not self.slider.isSliderDown():
            self.slider.setRange(0, int(p._player.duration()))
            self.slider.setValue(int(p._player.position()))
        self.time_lbl.setText(f"{_clock(p._player.position())} / {_clock(p._player.duration())}")
        self.play_btn.setText(p.play_btn.text())
        self.mute_btn.setText(p.mute_btn.text())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.exit_requested.emit()
        elif event.key() in (Qt.Key.Key_Space, Qt.Key.Key_K):
            self._p._toggle()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.exit_requested.emit()


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
        self._fs = None
        self._controls_sheet = ""
        self.screen = _screen_class()()
        self.screen.setFixedSize(SCREEN_W, SCREEN_H)            # (see fit_to: made to fit the card it is on)
        self.screen.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.screen.setStyleSheet("background: black;")
        # A frame round the picture: a black screen on a dark page has no edge
        # of its own, and a player whose picture and controls cannot be told
        # from the page looks like nothing was opened.
        self._frame = QFrame()
        self._frame.setObjectName("safe_video_frame")
        fl = QVBoxLayout(self._frame)
        fl.setContentsMargins(1, 1, 1, 1)
        fl.addWidget(self.screen)
        lay.addWidget(self._frame, 0, Qt.AlignmentFlag.AlignLeft)
        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setTextFormat(Qt.TextFormat.PlainText)
        self._note.setVisible(False)
        lay.addWidget(self._note)

        # Two rows, so that they fit the narrowest the chat gets: where it is in the video, then
        # the buttons.
        seek = QHBoxLayout()
        seek.setSpacing(6)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(lambda ms: self._player.setPosition(int(ms)))
        seek.addWidget(self.slider, 1)
        self.time_lbl = QLabel("0:00 / 0:00")
        seek.addWidget(self.time_lbl)
        lay.addLayout(seek)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.play_btn = QPushButton("⏸")
        self.play_btn.clicked.connect(self._toggle)
        row.addWidget(self.play_btn)
        self.mute_btn = QPushButton("🔊")
        self.mute_btn.clicked.connect(self._mute)
        row.addWidget(self.mute_btn)
        self.full_btn = QPushButton("⛶")
        self.full_btn.setToolTip(t("p2p.friends_fullscreen"))
        self.full_btn.clicked.connect(self.enter_fullscreen)
        row.addWidget(self.full_btn)
        row.addStretch(1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setToolTip(t("p2p.friends_video_close"))
        self.close_btn.clicked.connect(self.close_player)
        row.addWidget(self.close_btn)
        lay.addLayout(row)
        for btn in (self.play_btn, self.mute_btn, self.full_btn, self.close_btn):
            btn.setFixedSize(BUTTON_W, BUTTON_H)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.full_btn.setEnabled(False)                         # (until something has really come out)
        self._apply_style()

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

    # ── look ─────────────────────────────────────────────────────────────

    def fit_to(self, width: int):
        """Make the picture no wider than *width* (what a card has to give), at the proportions it
        was made for, and as it was when there is room."""
        w = max(120, min(SCREEN_W, int(width) - 4))             # (the frame's own edge and margin)
        self.screen.setFixedSize(w, max(68, round(w * SCREEN_H / SCREEN_W)))

    def control_style(self) -> str:
        """The style sheet the controls are drawn with (the full screen window wears it too)."""
        return self._controls_sheet

    def _apply_style(self):
        """The player draws its own controls from the theme's colours.

        Left to the generic button and slider styles, the buttons came out the
        colour of the page with a hairline edge, the slider's track all but
        disappeared, and the black picture had no edge on a dark page: the
        player was there and could not be seen. Every part is given a face, an
        edge and a hover that can be told from the background, in light and
        dark alike."""
        from PySide6.QtGui import QColor
        from ui.styles.theme import palette
        text, accent, page = QColor(palette("text")), palette("accent"), QColor(palette("bg"))

        def mix(k: float) -> str:
            """*k* of the text colour over the page: a tint that reads against
            the background in a light theme and a dark one alike (the theme's
            own border and elevated colours are meant to be barely there)."""
            return QColor(
                round(page.red() + (text.red() - page.red()) * k),
                round(page.green() + (text.green() - page.green()) * k),
                round(page.blue() + (text.blue() - page.blue()) * k)).name()

        face, edge, track = mix(0.14), mix(0.45), mix(0.30)
        self._frame.setStyleSheet(
            f"QFrame#safe_video_frame{{background:black;border:1px solid {edge};"
            f"border-radius:4px;}}")
        self._controls_sheet = (
            f"QPushButton{{background:{face};color:{text.name()};border:1px solid {edge};"
            f"border-radius:5px;padding:0;font-size:14px;}}"
            f"QPushButton:hover{{border-color:{accent};}}"
            f"QPushButton:pressed{{background:{track};}}"
            f"QPushButton:disabled{{color:{mix(0.45)};}}"
            f"QSlider::groove:horizontal{{height:6px;background:{track};border-radius:3px;}}"
            f"QSlider::sub-page:horizontal{{background:{accent};border-radius:3px;}}"
            f"QSlider::handle:horizontal{{width:14px;height:14px;margin:-5px 0;"
            f"border-radius:7px;background:{text.name()};border:1px solid {edge};}}"
            f"QSlider::groove:horizontal:disabled{{background:{face};}}"
            f"QLabel{{color:{palette('text_secondary')};}}")
        self.setStyleSheet(self._controls_sheet)

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
        self._sync_fullscreen()

    def _got_something(self):
        """A picture or a sound has really come out: it is a video after all."""
        if not self._loaded:
            self._loaded = True
            self._watchdog.stop()
            self.full_btn.setEnabled(True)

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
        self._sync_fullscreen()

    def _on_duration(self, ms: int):
        self.slider.setRange(0, int(ms))
        self.time_lbl.setText(f"{_clock(self._player.position())} / {_clock(ms)}")
        self._sync_fullscreen()

    def _on_state(self, state):
        playing = state == self._QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("⏸" if playing else "▶")
        self._sync_fullscreen()

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
        self.exit_fullscreen()                  # (the reason is told on the card, which is where it is read)
        self.full_btn.setEnabled(False)
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

    def is_fullscreen(self) -> bool:
        return self._fs is not None

    def _sync_fullscreen(self):
        if self._fs is not None:
            self._fs.sync()

    def enter_fullscreen(self):
        """Show this same player on a window that covers the screen the card is on. Only once a
        picture or a sound has come out (before that the watchdog is still deciding whether this is
        a video at all)."""
        if self._fs is not None or self._failed or not self._loaded:
            return
        try:
            win = _FullscreenWindow(self, self)
            win.exit_requested.connect(self.exit_fullscreen)
            host = self.window()
            screen = host.screen() if host is not None else None
            if screen is not None:
                win.setScreen(screen)
            self._fs = win
            self._player.setVideoOutput(win.screen)
            win.screen.videoSink().videoFrameChanged.connect(lambda _frame: self._got_something())
            win.showFullScreen()
            win.activateWindow()
            win.setFocus()
            win.sync()
            win.idle.start()
        except Exception:
            logger.debug("P2P friends: full screen could not start", exc_info=True)
            self.exit_fullscreen()

    def exit_fullscreen(self):
        """Back to the card: the player's own screen gets the picture again before the window goes."""
        win, self._fs = self._fs, None
        if win is None:
            return
        try:
            self._player.setVideoOutput(self.screen)
        except Exception:
            logger.debug("P2P friends: the picture could not be moved back", exc_info=True)
        win.hide()
        win.deleteLater()

    def close_player(self):
        """Stop, let go of the file, and go."""
        self.exit_fullscreen()
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
