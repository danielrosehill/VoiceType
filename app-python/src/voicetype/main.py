"""VoiceType entry point."""

from __future__ import annotations

import logging
import sys


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from PyQt6.QtWidgets import QApplication
    from .gui import VoiceTypeWindow

    app = QApplication(sys.argv)
    app.setApplicationName("VoiceType")
    app.setDesktopFileName("voicetype")

    window = VoiceTypeWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
