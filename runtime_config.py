"""Read and atomically update non-secret VPS runtime configuration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SESSION_DELIVERY_PROVIDERS = ("telegram", "vk")
AI_TEMPLATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,19}$")
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


def archive_delivery_providers() -> tuple[str, ...]:
    """Return the explicitly enabled automatic media archive destinations."""
    settings = CONFIG.get("archive_delivery")
    if not isinstance(settings, dict):
        raise RuntimeError(
            "config_vps.json: archive_delivery must be an object"
        )

    enabled = []
    for provider in SESSION_DELIVERY_PROVIDERS:
        value = settings.get(provider)
        if not isinstance(value, bool):
            raise RuntimeError(
                "config_vps.json: "
                f"archive_delivery.{provider} must be true or false"
            )
        if value:
            enabled.append(provider)
    return tuple(enabled)


def ai_image_edit_settings() -> dict:
    """Return validated AI image-edit settings and every configured template."""
    settings = CONFIG.get("ai_image_edit")
    if not isinstance(settings, dict):
        raise RuntimeError("config_vps.json: ai_image_edit must be an object")

    enabled = settings.get("enabled")
    if not isinstance(enabled, bool):
        raise RuntimeError(
            "config_vps.json: ai_image_edit.enabled must be true or false"
        )

    generator = settings.get("generator")
    if generator != "kie":
        raise RuntimeError(
            "config_vps.json: ai_image_edit.generator must be 'kie'"
        )

    raw_templates = settings.get("templates")
    if not isinstance(raw_templates, list):
        raise RuntimeError(
            "config_vps.json: ai_image_edit.templates must be an array"
        )

    templates = []
    seen_ids = set()
    for index, raw in enumerate(raw_templates):
        prefix = f"config_vps.json: ai_image_edit.templates[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{prefix} must be an object")
        template_id = raw.get("id")
        if not isinstance(template_id, str) or not AI_TEMPLATE_ID_RE.fullmatch(
            template_id
        ):
            raise RuntimeError(
                f"{prefix}.id must match {AI_TEMPLATE_ID_RE.pattern}"
            )
        if template_id in {"cancel", "print"}:
            raise RuntimeError(f"{prefix}.id is reserved")
        if template_id in seen_ids:
            raise RuntimeError(f"{prefix}.id is duplicated")
        seen_ids.add(template_id)

        button = raw.get("button")
        if not isinstance(button, str) or not button.strip():
            raise RuntimeError(f"{prefix}.button must be a non-empty string")
        prompt = raw.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise RuntimeError(f"{prefix}.prompt must be a non-empty string")
        if len(prompt) > 20_000:
            raise RuntimeError(f"{prefix}.prompt must not exceed 20000 characters")
        templates.append({
            "id": template_id,
            "button": button.strip(),
            "prompt": prompt.strip(),
        })

    if enabled and not templates:
        raise RuntimeError(
            "config_vps.json: enabled AI image edit needs at least one template"
        )
    return {
        "enabled": enabled,
        "generator": generator,
        "templates": tuple(templates),
    }


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
