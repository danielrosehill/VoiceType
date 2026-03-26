"""Virtual keyboard using Linux evdev UInput.

Creates a virtual keyboard device and types text by injecting key events.
Requires the user to be in the 'input' group (no root needed).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from evdev import UInput, ecodes

log = logging.getLogger(__name__)

# Regex: word "enter" at end of string, with optional trailing punct/whitespace
_ENTER_RE = re.compile(r"(?i)\s*\benter\b[^\w]*$")

# Character → (ecodes key, needs_shift)
_CHAR_MAP: dict[str, tuple[int, bool]] = {}


def _build_char_map() -> dict[str, tuple[int, bool]]:
    m: dict[str, tuple[int, bool]] = {}
    # Letters
    for c in "abcdefghijklmnopqrstuvwxyz":
        key = getattr(ecodes, f"KEY_{c.upper()}")
        m[c] = (key, False)
        m[c.upper()] = (key, True)
    # Numbers
    num_keys = [
        ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4, ecodes.KEY_5,
        ecodes.KEY_6, ecodes.KEY_7, ecodes.KEY_8, ecodes.KEY_9, ecodes.KEY_0,
    ]
    for i, digit in enumerate("1234567890"):
        m[digit] = (num_keys[i], False)
    # Shifted number symbols
    for sym, i in zip("!@#$%^&*()", range(10)):
        m[sym] = (num_keys[i], True)
    # Punctuation
    m[" "] = (ecodes.KEY_SPACE, False)
    m["\n"] = (ecodes.KEY_ENTER, False)
    m["\t"] = (ecodes.KEY_TAB, False)
    m["-"] = (ecodes.KEY_MINUS, False)
    m["_"] = (ecodes.KEY_MINUS, True)
    m["="] = (ecodes.KEY_EQUAL, False)
    m["+"] = (ecodes.KEY_EQUAL, True)
    m["["] = (ecodes.KEY_LEFTBRACE, False)
    m["{"] = (ecodes.KEY_LEFTBRACE, True)
    m["]"] = (ecodes.KEY_RIGHTBRACE, False)
    m["}"] = (ecodes.KEY_RIGHTBRACE, True)
    m["\\"] = (ecodes.KEY_BACKSLASH, False)
    m["|"] = (ecodes.KEY_BACKSLASH, True)
    m[";"] = (ecodes.KEY_SEMICOLON, False)
    m[":"] = (ecodes.KEY_SEMICOLON, True)
    m["'"] = (ecodes.KEY_APOSTROPHE, False)
    m['"'] = (ecodes.KEY_APOSTROPHE, True)
    m["`"] = (ecodes.KEY_GRAVE, False)
    m["~"] = (ecodes.KEY_GRAVE, True)
    m[","] = (ecodes.KEY_COMMA, False)
    m["<"] = (ecodes.KEY_COMMA, True)
    m["."] = (ecodes.KEY_DOT, False)
    m[">"] = (ecodes.KEY_DOT, True)
    m["/"] = (ecodes.KEY_SLASH, False)
    m["?"] = (ecodes.KEY_SLASH, True)
    return m


_CHAR_MAP = _build_char_map()

# Delay between keystrokes (seconds)
_KEY_DELAY = 0.008
_BACKSPACE_DELAY = 0.004


@dataclass
class VirtualKeyboard:
    """Virtual keyboard that types text via uinput."""

    voice_enter_enabled: bool = True
    uppercase_enabled: bool = False
    _current_text: str = ""
    _ui: UInput = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Request all KEY_* capabilities
        cap = {ecodes.EV_KEY: list(range(1, 256))}
        self._ui = UInput(cap, name="VoiceType")
        log.info("Virtual keyboard created (evdev UInput)")

    def close(self) -> None:
        self._ui.close()
        log.info("Virtual keyboard closed")

    # ── Low-level key injection ──────────────────────────────────────

    def _press_key(self, keycode: int) -> None:
        self._ui.write(ecodes.EV_KEY, keycode, 1)
        self._ui.syn()
        self._ui.write(ecodes.EV_KEY, keycode, 0)
        self._ui.syn()

    def _type_char(self, ch: str) -> None:
        entry = _CHAR_MAP.get(ch)
        if entry is None:
            log.warning("Unsupported character: %r", ch)
            return
        keycode, needs_shift = entry
        if needs_shift:
            self._ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1)
            self._ui.syn()
            self._press_key(keycode)
            self._ui.write(ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0)
            self._ui.syn()
        else:
            self._press_key(keycode)

    def _type_text(self, text: str) -> None:
        for ch in text:
            self._type_char(ch)
            time.sleep(_KEY_DELAY)

    def _press_backspace(self) -> None:
        self._press_key(ecodes.KEY_BACKSPACE)
        time.sleep(_BACKSPACE_DELAY)

    def _press_enter(self) -> None:
        self._press_key(ecodes.KEY_ENTER)

    # ── Transcript handling (smart incremental typing) ───────────────

    @property
    def current_text(self) -> str:
        return self._current_text

    def update_transcript(self, new_transcript: str) -> None:
        """Update the on-screen text incrementally."""
        text = new_transcript.upper() if self.uppercase_enabled else new_transcript

        if not text:
            self._clear_current_text()
            return

        old = self._current_text

        # Extension — just type the new suffix
        if text.startswith(old):
            new_chars = text[len(old):]
            if new_chars:
                self._type_text(new_chars)
                self._current_text = text
            return

        # Find common prefix
        common = 0
        for a, b in zip(old, text):
            if a != b:
                break
            common += 1

        # Backspace the divergent tail
        chars_to_delete = len(old) - common
        for _ in range(chars_to_delete):
            self._press_backspace()

        # Type the new tail
        new_tail = text[common:]
        if new_tail:
            self._type_text(new_tail)

        self._current_text = text

    def finalize_transcript(self) -> None:
        """Finalize current turn. Detect trailing 'enter' command."""
        if self.voice_enter_enabled:
            m = _ENTER_RE.search(self._current_text)
            if m:
                chars_to_delete = len(m.group())
                for _ in range(chars_to_delete):
                    self._press_backspace()
                self._press_enter()

        self._current_text = ""

    def _clear_current_text(self) -> None:
        for _ in self._current_text:
            self._press_backspace()
        self._current_text = ""
