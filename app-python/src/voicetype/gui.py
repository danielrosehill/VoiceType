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
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSystemTrayIcon,
    QMenu,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from .audio import AudioCapture
from .config import Config
from .stt_client import SttClient, TranscriptionResult
from .virtual_keyboard import VirtualKeyboard

log = logging.getLogger(__name__)

# ── Colors (light theme) ────────────────────────────────────────────

ACCENT = "#2D72E1"
RECORDING_RED = "#D83636"
SUCCESS_GREEN = "#2E9A4D"
SURFACE = "#F1F2F4"
TEXT_DIM = "#6B7280"
TEXT_PRIMARY = "#1E2229"


class _Signals(QObject):
    """Thread-safe bridge: STT callback → Qt main thread."""
    transcript_event = pyqtSignal(object)  # TranscriptionResult
    stt_error = pyqtSignal(str)
    stt_finished = pyqtSignal()


class VoiceTypeWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VoiceType")
        self.setFixedSize(480, 560)

        self._config = Config.load()
        self._is_recording = False
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
        self._update_status("Ready — press Start or hotkey")

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
        self._status_dot = QLabel("○")
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

        # Record button
        self._record_btn = QPushButton("Start Dictation")
        self._record_btn.setMinimumHeight(44)
        self._record_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._record_btn.clicked.connect(self._toggle_dictation)
        self._update_record_button()
        layout.addWidget(self._record_btn)

        # Hotkey hint
        hint = QLabel(f"Hotkey: {self._config.hotkey}")
        hint.setStyleSheet(f"color: {TEXT_DIM}; font-size: 12px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

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

        layout.addSpacing(8)

        # VAD toggle
        self._vad_check = QCheckBox("Voice Activity Detection (skip silence)")
        self._vad_check.setChecked(self._config.vad_enabled)
        layout.addWidget(self._vad_check)

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
        self._balance_label = QLabel("Click to check account balance")
        self._balance_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 13px;")
        billing_row.addWidget(check_btn)
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

    # ── System Tray ──────────────────────────────────────────────────

    def _setup_tray(self) -> None:
        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(QIcon.fromTheme("audio-input-microphone-muted"))
        self._tray.setToolTip("VoiceType — Idle")

        menu = QMenu()
        self._tray_toggle = menu.addAction("Start Dictation")
        self._tray_toggle.triggered.connect(self._toggle_dictation)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)
        self._tray.setContextMenu(menu)

        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_dictation()

    # ── Dictation Control ────────────────────────────────────────────

    def _toggle_dictation(self) -> None:
        if self._is_recording:
            self._stop_dictation()
        else:
            self._start_dictation()

    def _start_dictation(self) -> None:
        if not self._config.api_key:
            self._update_status("No API key — go to Settings")
            return

        self._is_recording = True
        self._current_transcript = ""
        self._update_record_button()
        self._update_status("Listening...")
        self._tray.setIcon(QIcon.fromTheme("audio-input-microphone"))
        self._tray.setToolTip("VoiceType — Recording")
        self._tray_toggle.setText("Stop Dictation")

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
        stt = SttClient(
            sample_rate=16000,
            api_key=self._config.api_key,
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

        # Stop audio first — this sends None to the queue, which makes
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
        self._update_status("Stopped")
        self._tray.setIcon(QIcon.fromTheme("audio-input-microphone-muted"))
        self._tray.setToolTip("VoiceType — Idle")
        self._tray_toggle.setText("Start Dictation")

    # ── STT Callbacks (from STT thread → Qt thread) ──────────────────

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
        self._update_status("Ready — press Start or hotkey")

    # ── UI Updates ───────────────────────────────────────────────────

    def _update_status(self, msg: str) -> None:
        self._status_label.setText(msg)
        color = RECORDING_RED if self._is_recording else TEXT_DIM
        self._status_dot.setText("●" if self._is_recording else "○")
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
        self._config.vad_enabled = self._vad_check.isChecked()
        self._config.save()
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

    # ── Cleanup ──────────────────────────────────────────────────────

    def _quit(self) -> None:
        if self._is_recording:
            self._stop_dictation()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if self._is_recording:
            self._stop_dictation()
        self._tray.hide()
        event.accept()
