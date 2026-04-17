"""PyQt6 GUI for VoiceType."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from functools import partial
from typing import Optional

import httpx
from PyQt6.QtCore import QTimer, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QMenu,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QCheckBox,
    QGroupBox,
    QFormLayout,
)

from .audio import AudioCapture
from .config import Config
from .cost_dialog import CostDialog
from .hotkeys import HotkeyListener
from .sounds import play_start, play_stop, play_pause, play_resume
from .stt_client import SttClient, TranscriptionResult
from .virtual_keyboard import VirtualKeyboard

# Deepgram streaming models exposed in the settings UI
AVAILABLE_MODELS: list[tuple[str, str]] = [
    ("nova-3", "Nova-3 (English) — best for dictation, supports keyterms"),
    ("nova-3-multi", "Nova-3 (Multilingual) — supports keyterms"),
    ("flux-general-en", "Flux (English) — conversational, ultra-low latency"),
    ("flux-general-multi", "Flux (Multilingual)"),
]

log = logging.getLogger(__name__)

# ── Colors (light theme) ────────────────────────────────────────────

ACCENT = "#2D72E1"
RECORDING_RED = "#D83636"
SUCCESS_GREEN = "#2E9A4D"
PAUSED_AMBER = "#D4880F"
SURFACE = "#F1F2F4"
TEXT_DIM = "#6B7280"
TEXT_PRIMARY = "#1E2229"


def _make_circle_icon(color: str, size: int = 64) -> QIcon:
    """Create a simple filled-circle icon for the system tray."""
    pm = QPixmap(size, size)
    pm.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    margin = size // 8
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()
    return QIcon(pm)


# Pre-build tray icons
_ICON_IDLE = None
_ICON_RECORDING = None
_ICON_PAUSED = None


def _ensure_icons() -> None:
    global _ICON_IDLE, _ICON_RECORDING, _ICON_PAUSED
    if _ICON_IDLE is None:
        _ICON_IDLE = _make_circle_icon(TEXT_DIM)
        _ICON_RECORDING = _make_circle_icon(RECORDING_RED)
        _ICON_PAUSED = _make_circle_icon(PAUSED_AMBER)


class _Signals(QObject):
    """Thread-safe bridge: STT callback -> Qt main thread."""
    transcript_event = pyqtSignal(object)  # TranscriptionResult
    stt_error = pyqtSignal(str)
    stt_finished = pyqtSignal()


class VoiceTypeWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        _ensure_icons()
        self.setWindowTitle("VoiceType")
        self.setMinimumSize(480, 720)
        self.resize(480, 760)

        self._config = Config.load()
        self._is_recording = False
        self._is_paused = False
        self._keyboard: Optional[VirtualKeyboard] = None
        self._audio: Optional[AudioCapture] = None
        self._stt_thread: Optional[threading.Thread] = None
        self._stt_loop: Optional[asyncio.AbstractEventLoop] = None
        self._signals = _Signals()
        self._signals.transcript_event.connect(self._on_transcript_event)
        self._signals.stt_error.connect(self._on_stt_error)
        self._signals.stt_finished.connect(self._on_stt_finished)
        self._transcript_lines: list[str] = []
        self._current_transcript = ""

        self._build_ui()
        self._setup_tray()
        self._setup_hotkeys()
        self._update_status("Ready -- press Start or hotkey")

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QWidget()
        header.setStyleSheet(f"background: {SURFACE};")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(20, 14, 20, 14)
        self._status_dot = QLabel("\u25cb")
        self._status_dot.setStyleSheet(f"color: {TEXT_DIM}; font-size: 14px;")
        title = QLabel("VoiceType")
        title.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 22px; font-weight: bold;")
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px;")
        h_layout.addWidget(self._status_dot)
        h_layout.addWidget(title)
        h_layout.addStretch()
        h_layout.addWidget(self._status_label)
        layout.addWidget(header)

        # Tabs
        tabs = QTabWidget()
        tabs.addTab(self._build_main_tab(), "Dictation")
        tabs.addTab(self._build_settings_tab(), "Settings")
        layout.addWidget(tabs)

    def _build_main_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(20, 12, 20, 12)

        # Button row
        btn_row = QHBoxLayout()

        self._record_btn = QPushButton("Start Dictation")
        self._record_btn.setMinimumHeight(44)
        self._record_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._record_btn.clicked.connect(self._toggle_dictation)
        self._update_record_button()
        btn_row.addWidget(self._record_btn, stretch=3)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setMinimumHeight(44)
        self._pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pause_btn.setEnabled(False)
        self._pause_btn.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid #CCC; border-radius: 8px; "
            "font-size: 14px;"
        )
        self._pause_btn.clicked.connect(self._toggle_pause)
        btn_row.addWidget(self._pause_btn, stretch=1)

        layout.addLayout(btn_row)

        # Hotkey hint
        hint_text = self._hotkey_hint_text()
        self._hotkey_hint = QLabel(hint_text)
        self._hotkey_hint.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        self._hotkey_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hotkey_hint.setWordWrap(True)
        layout.addWidget(self._hotkey_hint)

        layout.addSpacing(8)

        # Transcript label
        t_label = QLabel("Transcript")
        t_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        layout.addWidget(t_label)

        # Transcript area
        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setStyleSheet(
            f"background: {SURFACE}; border: 1px solid #E5E7EB; border-radius: 8px; "
            f"padding: 12px; font-size: 14px; color: {TEXT_PRIMARY};"
        )
        self._transcript.setPlaceholderText("Transcripts will appear here")
        layout.addWidget(self._transcript)

        return w

    def _build_settings_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(20, 12, 20, 12)

        # API Key
        layout.addWidget(self._section_label("Deepgram API"))
        layout.addWidget(QLabel("API Key"))
        self._api_key_input = QLineEdit(self._config.api_key)
        self._api_key_input.setPlaceholderText("dg_...")
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._api_key_input)

        # Project ID
        layout.addWidget(QLabel("Project ID (for billing)"))
        self._project_id_input = QLineEdit(self._config.project_id)
        self._project_id_input.setPlaceholderText("Project ID")
        layout.addWidget(self._project_id_input)

        # API Key ID (accessor) — optional, scopes cost queries to this key
        layout.addWidget(QLabel("API Key ID (optional, scopes costs to this key)"))
        self._api_key_id_input = QLineEdit(self._config.api_key_id)
        self._api_key_id_input.setPlaceholderText("Accessor ID — leave blank for project-wide costs")
        layout.addWidget(self._api_key_id_input)

        # Model selector
        layout.addWidget(QLabel("Model"))
        self._model_combo = QComboBox()
        for value, label in AVAILABLE_MODELS:
            self._model_combo.addItem(label, value)
        # Select current
        idx = next(
            (i for i, (v, _) in enumerate(AVAILABLE_MODELS) if v == self._config.model),
            0,
        )
        self._model_combo.setCurrentIndex(idx)
        layout.addWidget(self._model_combo)

        # Keyterms (Nova-3 only)
        kt_label = QLabel("Keyterms — one per line, biases Nova-3 recognition")
        kt_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        layout.addWidget(kt_label)
        self._keyterms_input = QTextEdit()
        self._keyterms_input.setPlainText(self._config.keyterms)
        self._keyterms_input.setPlaceholderText(
            "ComfyUI\nKubernetes\nDaniel Rosehill\n(proper nouns, jargon, names)"
        )
        self._keyterms_input.setFixedHeight(80)
        layout.addWidget(self._keyterms_input)

        layout.addSpacing(8)

        # VAD toggle
        self._vad_check = QCheckBox("Voice Activity Detection (skip silence)")
        self._vad_check.setChecked(self._config.vad_enabled)
        layout.addWidget(self._vad_check)

        # Sound toggle
        self._sound_check = QCheckBox("Play audio feedback on start/stop")
        self._sound_check.setChecked(self._config.sound_enabled)
        layout.addWidget(self._sound_check)

        layout.addSpacing(8)

        # ── Hotkeys ──
        hotkey_group = QGroupBox("Hotkeys")
        hk_layout = QFormLayout(hotkey_group)

        self._hotkey_input = QLineEdit(self._config.hotkey)
        self._hotkey_input.setPlaceholderText("e.g. F13")
        hk_layout.addRow("Toggle (start/stop):", self._hotkey_input)

        self._hotkey_start_input = QLineEdit(self._config.hotkey_start)
        self._hotkey_start_input.setPlaceholderText("e.g. F14 (optional)")
        hk_layout.addRow("Start only:", self._hotkey_start_input)

        self._hotkey_stop_input = QLineEdit(self._config.hotkey_stop)
        self._hotkey_stop_input.setPlaceholderText("e.g. F15 (optional)")
        hk_layout.addRow("Stop only:", self._hotkey_stop_input)

        self._hotkey_pause_input = QLineEdit(self._config.hotkey_pause)
        self._hotkey_pause_input.setPlaceholderText("e.g. F16 (optional)")
        hk_layout.addRow("Pause/Resume:", self._hotkey_pause_input)

        layout.addWidget(hotkey_group)

        layout.addSpacing(4)

        # ── Push-to-Talk ──
        ptt_group = QGroupBox("Push-to-Talk")
        ptt_layout = QFormLayout(ptt_group)

        self._ptt_check = QCheckBox("Enable push-to-talk mode")
        self._ptt_check.setChecked(self._config.push_to_talk)
        ptt_layout.addRow(self._ptt_check)

        self._ptt_key_input = QLineEdit(self._config.push_to_talk_key)
        self._ptt_key_input.setPlaceholderText("e.g. F13")
        ptt_layout.addRow("PTT key:", self._ptt_key_input)

        layout.addWidget(ptt_group)

        layout.addSpacing(8)

        # Save button
        save_btn = QPushButton("Save Settings")
        save_btn.setStyleSheet(
            f"background: {ACCENT}; color: white; border-radius: 6px; "
            "padding: 10px; font-size: 14px;"
        )
        save_btn.clicked.connect(self._save_config)
        layout.addWidget(save_btn)

        layout.addSpacing(12)

        # Billing section
        layout.addWidget(self._section_label("Billing"))
        billing_row = QHBoxLayout()
        check_btn = QPushButton("Check Balance")
        check_btn.setStyleSheet(
            f"background: {ACCENT}; color: white; border-radius: 6px; padding: 6px 12px;"
        )
        check_btn.clicked.connect(self._check_balance)
        costs_btn = QPushButton("View Costs")
        costs_btn.setStyleSheet(
            f"background: {ACCENT}; color: white; border-radius: 6px; padding: 6px 12px;"
        )
        costs_btn.clicked.connect(self._open_cost_dialog)
        self._balance_label = QLabel("Click to check account balance")
        self._balance_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px;")
        billing_row.addWidget(check_btn)
        billing_row.addWidget(costs_btn)
        billing_row.addWidget(self._balance_label)
        billing_row.addStretch()
        layout.addLayout(billing_row)

        layout.addStretch()
        return w

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {ACCENT}; font-size: 14px; font-weight: bold;")
        return lbl

    def _hotkey_hint_text(self) -> str:
        parts = []
        if self._config.push_to_talk:
            parts.append(f"Push-to-talk: hold {self._config.push_to_talk_key}")
        elif self._config.hotkey:
            parts.append(f"Toggle: {self._config.hotkey}")
        if self._config.hotkey_start:
            parts.append(f"Start: {self._config.hotkey_start}")
        if self._config.hotkey_stop:
            parts.append(f"Stop: {self._config.hotkey_stop}")
        if self._config.hotkey_pause:
            parts.append(f"Pause: {self._config.hotkey_pause}")
        return "  |  ".join(parts) if parts else "No hotkey configured"

    # ── System Tray ──────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(_ICON_IDLE)
        self._tray.setToolTip("VoiceType -- Idle")

        menu = QMenu()
        self._tray_toggle = menu.addAction("Start Dictation")
        self._tray_toggle.triggered.connect(self._toggle_dictation)
        self._tray_pause = menu.addAction("Pause")
        self._tray_pause.setEnabled(False)
        self._tray_pause.triggered.connect(self._toggle_pause)
        menu.addSeparator()
        show_action = menu.addAction("Show Window")
        show_action.triggered.connect(self._show_window)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_dictation()

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _update_tray_state(self) -> None:
        if self._is_recording and self._is_paused:
            self._tray.setIcon(_ICON_PAUSED)
            self._tray.setToolTip("VoiceType -- Paused")
            self._tray_toggle.setText("Stop Dictation")
            self._tray_pause.setText("Resume")
            self._tray_pause.setEnabled(True)
        elif self._is_recording:
            self._tray.setIcon(_ICON_RECORDING)
            self._tray.setToolTip("VoiceType -- Recording")
            self._tray_toggle.setText("Stop Dictation")
            self._tray_pause.setText("Pause")
            self._tray_pause.setEnabled(True)
        else:
            self._tray.setIcon(_ICON_IDLE)
            self._tray.setToolTip("VoiceType -- Idle")
            self._tray_toggle.setText("Start Dictation")
            self._tray_pause.setEnabled(False)

    # ── Hotkeys ──────────────────────────────────────────────────────

    def _setup_hotkeys(self) -> None:
        self._hotkey_listener = HotkeyListener()

        self._hotkey_listener.configure(
            toggle_key=self._config.hotkey if not self._config.push_to_talk else "",
            start_key=self._config.hotkey_start,
            stop_key=self._config.hotkey_stop,
            pause_key=self._config.hotkey_pause,
            ptt_key=self._config.push_to_talk_key if self._config.push_to_talk else "",
            ptt_mode=self._config.push_to_talk,
        )

        self._hotkey_listener.signals.toggle.connect(self._toggle_dictation)
        self._hotkey_listener.signals.start.connect(self._hotkey_start)
        self._hotkey_listener.signals.stop.connect(self._hotkey_stop)
        self._hotkey_listener.signals.pause.connect(self._toggle_pause)
        self._hotkey_listener.signals.ptt_pressed.connect(self._ptt_pressed)
        self._hotkey_listener.signals.ptt_released.connect(self._ptt_released)

        self._hotkey_listener.start()

    def _hotkey_start(self) -> None:
        if not self._is_recording:
            self._start_dictation()

    def _hotkey_stop(self) -> None:
        if self._is_recording:
            self._stop_dictation()

    def _ptt_pressed(self) -> None:
        if not self._is_recording:
            self._start_dictation()

    def _ptt_released(self) -> None:
        if self._is_recording:
            self._stop_dictation()

    # ── Dictation Control ────────────────────────────────────────────

    def _toggle_dictation(self) -> None:
        if self._is_recording:
            self._stop_dictation()
        else:
            self._start_dictation()

    def _toggle_pause(self) -> None:
        if not self._is_recording:
            return

        if self._is_paused:
            self._resume_dictation()
        else:
            self._pause_dictation()

    def _start_dictation(self) -> None:
        if not self._config.api_key:
            self._update_status("No API key -- go to Settings")
            return

        self._is_recording = True
        self._is_paused = False
        self._current_transcript = ""
        self._update_record_button()
        self._update_pause_button()
        self._update_status("Listening...")
        self._update_tray_state()

        if self._config.sound_enabled:
            play_start()

        # Create keyboard
        self._keyboard = VirtualKeyboard(
            voice_enter_enabled=True,
            uppercase_enabled=False,
        )

        # Create async event loop in a thread
        self._stt_loop = asyncio.new_event_loop()
        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)

        # Create audio capture
        self._audio = AudioCapture(
            audio_queue=audio_queue,
            loop=self._stt_loop,
            vad_enabled=self._config.vad_enabled,
        )
        self._audio.start()

        # Start STT in background thread
        keyterms = [
            ln.strip()
            for ln in (self._config.keyterms or "").splitlines()
            if ln.strip()
        ]
        stt = SttClient(
            sample_rate=16000,
            api_key=self._config.api_key,
            model=self._config.model or "nova-3",
            keyterms=keyterms,
        )

        def _run_stt() -> None:
            assert self._stt_loop is not None
            asyncio.set_event_loop(self._stt_loop)
            try:
                self._stt_loop.run_until_complete(
                    stt.run(audio_queue, self._on_transcription)
                )
                self._signals.stt_finished.emit()
            except Exception as e:
                log.error("STT error: %s", e)
                self._signals.stt_error.emit(str(e))

        self._stt_thread = threading.Thread(target=_run_stt, daemon=True)
        self._stt_thread.start()

    def _stop_dictation(self) -> None:
        self._is_recording = False
        self._is_paused = False

        if self._config.sound_enabled:
            play_stop()

        # Stop audio first -- this sends None to the queue, which makes
        # the STT client send CloseStream and shut down gracefully
        if self._audio is not None:
            self._audio.stop()
            self._audio = None

        # Wait briefly for STT thread to finish
        if self._stt_thread is not None:
            self._stt_thread.join(timeout=3.0)
            self._stt_thread = None

        if self._stt_loop is not None:
            self._stt_loop.call_soon_threadsafe(self._stt_loop.stop)
            self._stt_loop = None

        # Finalize and close keyboard
        if self._keyboard is not None:
            if self._current_transcript:
                self._transcript_lines.append(self._current_transcript)
                self._current_transcript = ""
            self._keyboard.close()
            self._keyboard = None

        self._update_record_button()
        self._update_pause_button()
        self._update_status("Stopped")
        self._update_tray_state()

    def _pause_dictation(self) -> None:
        if not self._is_recording or self._is_paused:
            return
        self._is_paused = True

        if self._audio is not None:
            self._audio.set_paused(True)

        if self._config.sound_enabled:
            play_pause()

        self._update_record_button()
        self._update_pause_button()
        self._update_status("Paused")
        self._update_tray_state()

    def _resume_dictation(self) -> None:
        if not self._is_recording or not self._is_paused:
            return
        self._is_paused = False

        if self._audio is not None:
            self._audio.set_paused(False)

        if self._config.sound_enabled:
            play_resume()

        self._update_record_button()
        self._update_pause_button()
        self._update_status("Listening...")
        self._update_tray_state()

    # ── STT Callbacks (from STT thread -> Qt thread) ──────────────────

    def _on_transcription(self, result: TranscriptionResult) -> None:
        """Called from the STT thread."""
        self._signals.transcript_event.emit(result)

    def _on_transcript_event(self, result: TranscriptionResult) -> None:
        """Called on the Qt main thread."""
        if self._keyboard is None:
            return

        if result.event == "EndOfTurn":
            self._keyboard.finalize_transcript()
            if self._current_transcript:
                self._transcript_lines.append(self._current_transcript)
            self._current_transcript = ""
        else:
            self._keyboard.update_transcript(result.transcript)
            self._current_transcript = result.transcript

        self._refresh_transcript_display()

    def _on_stt_error(self, error: str) -> None:
        log.error("STT error received: %s", error)
        if self._is_recording:
            self._stop_dictation()
        self._update_status(f"Error: {error}")

    def _on_stt_finished(self) -> None:
        if self._is_recording:
            self._stop_dictation()
        self._update_status("Ready -- press Start or hotkey")

    # ── UI Updates ───────────────────────────────────────────────────

    def _update_status(self, msg: str) -> None:
        self._status_label.setText(msg)
        if self._is_recording and self._is_paused:
            color = PAUSED_AMBER
        elif self._is_recording:
            color = RECORDING_RED
        else:
            color = TEXT_DIM
        self._status_dot.setText("\u25cf" if self._is_recording else "\u25cb")
        self._status_dot.setStyleSheet(f"color: {color}; font-size: 14px;")
        self._status_label.setStyleSheet(f"color: {color}; font-size: 13px;")

    def _update_record_button(self) -> None:
        if self._is_recording:
            self._record_btn.setText("Stop Dictation")
            self._record_btn.setStyleSheet(
                f"background: {RECORDING_RED}; color: white; border-radius: 8px; "
                "font-size: 16px; font-weight: bold;"
            )
        else:
            self._record_btn.setText("Start Dictation")
            self._record_btn.setStyleSheet(
                f"background: {SUCCESS_GREEN}; color: white; border-radius: 8px; "
                "font-size: 16px; font-weight: bold;"
            )

    def _update_pause_button(self) -> None:
        if self._is_recording and self._is_paused:
            self._pause_btn.setEnabled(True)
            self._pause_btn.setText("Resume")
            self._pause_btn.setStyleSheet(
                f"background: {PAUSED_AMBER}; color: white; border-radius: 8px; "
                "font-size: 14px;"
            )
        elif self._is_recording:
            self._pause_btn.setEnabled(True)
            self._pause_btn.setText("Pause")
            self._pause_btn.setStyleSheet(
                f"background: {SURFACE}; border: 1px solid #CCC; border-radius: 8px; "
                "font-size: 14px;"
            )
        else:
            self._pause_btn.setEnabled(False)
            self._pause_btn.setText("Pause")
            self._pause_btn.setStyleSheet(
                f"background: {SURFACE}; border: 1px solid #CCC; border-radius: 8px; "
                "font-size: 14px;"
            )

    def _refresh_transcript_display(self) -> None:
        lines = self._transcript_lines[-50:]
        if self._current_transcript:
            lines = lines + [self._current_transcript]
        self._transcript.setPlainText("\n".join(lines))
        # Scroll to bottom
        sb = self._transcript.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    # ── Settings ─────────────────────────────────────────────────────

    def _save_config(self) -> None:
        self._config.api_key = self._api_key_input.text().strip()
        self._config.project_id = self._project_id_input.text().strip()
        self._config.api_key_id = self._api_key_id_input.text().strip()
        self._config.model = self._model_combo.currentData() or "nova-3"
        self._config.keyterms = self._keyterms_input.toPlainText().strip()
        self._config.vad_enabled = self._vad_check.isChecked()
        self._config.sound_enabled = self._sound_check.isChecked()
        self._config.hotkey = self._hotkey_input.text().strip()
        self._config.hotkey_start = self._hotkey_start_input.text().strip()
        self._config.hotkey_stop = self._hotkey_stop_input.text().strip()
        self._config.hotkey_pause = self._hotkey_pause_input.text().strip()
        self._config.push_to_talk = self._ptt_check.isChecked()
        self._config.push_to_talk_key = self._ptt_key_input.text().strip()
        self._config.save()

        # Reconfigure hotkeys
        self._hotkey_listener.stop()
        self._setup_hotkeys()

        # Update hint text
        self._hotkey_hint.setText(self._hotkey_hint_text())

        self._update_status("Settings saved")

    def _check_balance(self) -> None:
        if not self._config.api_key or not self._config.project_id:
            self._balance_label.setText("Set API key and Project ID first")
            return

        self._balance_label.setText("Checking...")

        def _fetch() -> None:
            try:
                url = f"https://api.deepgram.com/v1/projects/{self._config.project_id}/balances"
                resp = httpx.get(
                    url,
                    headers={"Authorization": f"Token {self._config.api_key}"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                balances = data.get("balances", [])
                if balances:
                    parts = [f"${b['amount']:.2f} ({b['units']})" for b in balances]
                    text = "  |  ".join(parts)
                else:
                    text = "No balance data"
            except Exception as e:
                text = f"Error: {e}"

            # Update label on Qt thread
            QTimer.singleShot(0, lambda: self._balance_label.setText(text))

        threading.Thread(target=_fetch, daemon=True).start()

    def _open_cost_dialog(self) -> None:
        dlg = CostDialog(
            project_id=self._config.project_id,
            api_key=self._config.api_key,
            accessor=self._config.api_key_id,
            parent=self,
        )
        dlg.exec()

    # ── Cleanup ──────────────────────────────────────────────────────

    def _quit(self) -> None:
        if self._is_recording:
            self._stop_dictation()
        self._hotkey_listener.stop()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        # Minimize to tray instead of quitting
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "VoiceType",
            "Still running in the system tray",
            QSystemTrayIcon.MessageIcon.Information,
            2000,
        )
