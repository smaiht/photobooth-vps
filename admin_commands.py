"""Parsing and user-facing descriptions for provider-neutral admin commands."""

import re
from datetime import date

import event_access

ParsedCommand = tuple[str, dict | None]

DEFAULT_UNBLOCK_SESSIONS = 1
MAX_UNBLOCK_SESSIONS = 1000
EVENT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.+)$")
CAMERA_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# These are the only named admin commands understood by the VPS. Everything
# else in the form /field value is forwarded as a camera setting and validated
# by the booth against its current config_camera.json.
KNOWN_COMMANDS = {
    "/run": "run",
    "/status": "status",
    "/unblock": "unblock",
    "/block": "unblock",
    "/logs": "send_logs",
    "/get_config": "get_config",
    "/clear_logs": "clear_logs",
    "/restart": "restart",
    "/update": "update",
    "/event": "set_event",
}

HELP_MESSAGE = (
    "Не понял команду. Доступные команды:\n\n"
    + "\n\n".join(KNOWN_COMMANDS)
    + "\n\nНастройка камеры, например: /iso 100"
)


def _parse_event(
    argument: str | None,
) -> ParsedCommand:
    if not argument:
        raise ValueError(
            "Использование: /event 2026-08-17 Свадьба Ивановых"
        )
    name = event_access.normalize_name(argument)
    if name == event_access.TECHNICAL_EVENT_NAME:
        return "set_event", {"name": name}

    matched = EVENT_NAME_RE.fullmatch(name)
    if not matched or not matched.group(2).strip():
        raise ValueError(
            "Название должно начинаться с даты: "
            "/event 2026-08-17 Свадьба Ивановых"
        )
    try:
        date.fromisoformat(matched.group(1))
    except ValueError as exc:
        raise ValueError("В начале названия указана неверная дата") from exc
    event_access.validate_configuration()
    return "set_event", {"name": name}


def _parse_unblock(argument: str | None) -> ParsedCommand:
    if argument is None:
        sessions = DEFAULT_UNBLOCK_SESSIONS
    elif not re.fullmatch(r"[0-9]+", argument):
        raise ValueError(
            "Использование: /unblock [0 или число от 1 до 1000]"
        )
    else:
        sessions = int(argument)
    if not 0 <= sessions <= MAX_UNBLOCK_SESSIONS:
        raise ValueError(
            "Количество сессий должно быть от 0 до 1000; "
            "0 сразу блокирует запуск"
        )
    return "unblock", {"sessions": sessions}


def _parse_block(argument: str | None) -> ParsedCommand:
    if argument is not None:
        raise ValueError("Использование: /block")
    return "unblock", {"sessions": 0}


def parse(text: str) -> ParsedCommand | None:
    """Parse and validate one admin message without performing side effects."""
    parts = (text or "").strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None

    command_name = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) == 2 else None
    known_command = KNOWN_COMMANDS.get(command_name)

    if known_command == "set_event":
        return _parse_event(argument)
    if command_name == "/block":
        return _parse_block(argument)
    if known_command == "unblock":
        return _parse_unblock(argument)
    if known_command:
        return known_command, None

    # /start is consumed by a provider adapter and must never be treated as a
    # camera field if this parser is called directly.
    if command_name == "/start":
        return None

    field = command_name.removeprefix("/")
    if not CAMERA_FIELD_RE.fullmatch(field):
        return None
    if argument is None:
        raise ValueError(f"Использование: /{field} значение")
    return "set_camera_config", {"field": field, "value": argument}


def sent_message(command: str, data: dict | None) -> str:
    if command == "set_event":
        return f"⏳ Переключаю мероприятие на будке и VPS: {data['name']}"
    if command == "unblock":
        if data["sessions"] == 0:
            return (
                "⏳ Кафе: блокирую запуск новых сессий; "
                "ожидаю подтверждение будки"
            )
        return (
            "⏳ Кафе: задаю остаток разрешённых сессий — "
            f"{data['sessions']}; ожидаю подтверждение будки"
        )
    if command == "set_camera_config":
        return (
            f"⏳ Камера: {data['field']} → {data['value']}; "
            "ожидаю подтверждение будки"
        )
    if command == "get_config":
        return "⏳ Запрашиваю конфиги фотобудки..."
    return f"⏳ {command}: команда отправлена"


def failed_message(command: str, error: Exception) -> str:
    label = {
        "set_event": "Команда смены мероприятия не отправлена",
        "unblock": "Изменение блокировки не отправлено",
        "set_camera_config": "Настройка камеры не отправлена",
    }.get(command, "Команда не отправлена")
    return f"❌ {label}: {error}"
