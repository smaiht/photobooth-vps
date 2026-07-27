"""Read and atomically update non-secret VPS runtime configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
_configured_path = Path(os.environ.get("VPS_CONFIG", "config_vps.json"))
CONFIG_PATH = (
    _configured_path
    if _configured_path.is_absolute()
    else PROJECT_ROOT / _configured_path
)


def load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CONFIG = load()


def _required_text(key: str) -> str:
    value = CONFIG.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"config_vps.json: {key} must be a non-empty string")
    return value.strip()


def yadisk_folder() -> str:
    return _required_text("yadisk_folder")


def control_folder() -> str:
    return _required_text("yadisk_control_folder")


def updates_folder() -> str:
    return _required_text("yadisk_updates_folder")


def save_event(name: str) -> None:
    data = load()
    data["yadisk_folder"] = name
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    temporary.replace(CONFIG_PATH)
    CONFIG["yadisk_folder"] = name


def read_bytes() -> bytes:
    return CONFIG_PATH.read_bytes()
