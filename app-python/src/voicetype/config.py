"""Persistent configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = Path(xdg) / "voicetype"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return _config_dir() / "config.json"


@dataclass
class Config:
    api_key: str = ""
    hotkey: str = "F13"
    project_id: str = ""
    vad_enabled: bool = True

    def save(self) -> None:
        path = _config_path()
        path.write_text(json.dumps(asdict(self), indent=2))
        path.chmod(0o600)
        log.info("Config saved to %s", path)

    @classmethod
    def load(cls) -> Config:
        path = _config_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception as e:
            log.warning("Failed to load config: %s", e)
            return cls()
