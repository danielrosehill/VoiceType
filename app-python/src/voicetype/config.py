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


# Legacy field names that may exist in older config files
_FIELD_ALIASES = {
    "hotkey_code": "hotkey",
}


@dataclass
class Config:
    api_key: str = ""
    hotkey: str = "F13"
    hotkey_start: str = ""
    hotkey_stop: str = ""
    hotkey_pause: str = ""
    project_id: str = ""
    api_key_id: str = ""  # Accessor ID for scoping cost queries to this key
    model: str = "nova-3"
    keyterms: str = ""  # One keyterm per line; only used with Nova-3 models
    vad_enabled: bool = True
    push_to_talk: bool = False
    push_to_talk_key: str = "F13"
    sound_enabled: bool = True

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
            # Apply legacy field aliases
            for old_name, new_name in _FIELD_ALIASES.items():
                if old_name in data and new_name not in data:
                    data[new_name] = data.pop(old_name)
                elif old_name in data:
                    del data[old_name]
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except Exception as e:
            log.warning("Failed to load config: %s", e)
            return cls()
