"""Parsing and user-facing descriptions for provider-neutral admin commands."""

import re
from datetime import date

import event_access

ParsedCommand = tuple[str, dict | None]

DEFAULT_UNBLOCK_SESSIONS = 1
MAX_UNBLOCK_SESSIONS = 1000
EVENT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.+)$")
CONFIG_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TEMPLATE_PACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Operator-facing command names are intentionally duplicated from the booth's
# config_camera.json.  The help must be complete even when the booth is offline;
# keep these tuples in sync when a preset or a public camera field is added.
LIGHT_PRESETS = (
    ("sun", "Яркое солнце"),
    ("cloudy", "Улица, пасмурно"),
    ("evening", "Улица, тёмный вечер"),
    ("indoor", "Помещение со светом"),
    ("indoor_dark", "Помещение, темно"),
)
LIGHT_PRESET_NAMES = frozenset(name for name, _label in LIGHT_PRESETS)

CAMERA_SETTING_FIELDS = (
    "image_quality",
    "ae_mode",
    "shutter_type",
    "av",
    "tv",
    "iso",
    "white_balance",
    "color_temperature",
    "picture_style",
    "evf_af_mode",
    "af_mode",
    "subject_tracking",
    "evf_view_type",
    "continuous_af",
    "eye_detection_af",
    "focus_before_capture",
    "focus_delay",
    "disable_auto_power_off",
    "min_free_disk_gib",
    "evf_keep_camera_screen",
    "drive_mode",
    "color_space",
    "lock_camera_ui",
    "lock_mode_dial",
)
CAMERA_SETTING_FIELD_NAMES = frozenset(CAMERA_SETTING_FIELDS)

# These are the only named admin commands understood by the VPS. Camera fields
# use the explicit allowlist above; the booth still performs final validation
# against its current config_camera.json.
KNOWN_COMMANDS = {
    "/run": "run",
    "/status": "status",
    "/printer_info": "printer_info",
    "/print_queue": "print_queue",
    "/clear_print_queue": "clear_print_queue",
    "/clear_photos": "clear_photos",
    "/clear_print_jobs": "clear_print_jobs",
    "/unblock": "unblock",
    "/block": "unblock",
    "/light": "set_camera_preset",
    "/logs": "send_logs",
    "/get_config": "get_config",
    "/clear_logs": "clear_logs",
    "/restart": "restart",
    "/event": "set_event",
    "/template": "set_template_pack",
}

PRESET_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

_GENERAL_HELP_COMMANDS = tuple(
    "/template <pack>" if name == "/template" else name
    for name in KNOWN_COMMANDS if name != "/light"
)
_PRESET_HELP_COMMANDS = tuple(
    f"/light {name} — {label}" for name, label in LIGHT_PRESETS
)
_CAMERA_HELP_COMMANDS = tuple(
    f"/{field} <значение>" for field in CAMERA_SETTING_FIELDS
)
_APP_HELP_COMMAND = (
    "/<поле config_app> <значение> — только из "
    "_admin_editable_fields"
)

HELP_MESSAGE = (
    "Не понял команду. Доступные команды:\n\n"
    + "\n".join(_GENERAL_HELP_COMMANDS)
    + "\n\nПресеты света:\n"
    + "\n".join(_PRESET_HELP_COMMANDS)
    + "\n\nНастройки камеры:\n"
    + "\n".join(_CAMERA_HELP_COMMANDS)
    + "\n\nНастройки приложения:\n"
    + _APP_HELP_COMMAND
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


def _parse_light(argument: str | None) -> ParsedCommand:
    available = "\n".join(
        f"/light {name} — {label}" for name, label in LIGHT_PRESETS
    )
    if not argument:
        raise ValueError(
            "Выберите пресет света:\n" + available
        )
    name = argument.strip().lower()
    if not PRESET_NAME_RE.fullmatch(name) or name not in LIGHT_PRESET_NAMES:
        raise ValueError(
            "Неизвестный пресет света. Доступно:\n" + available
        )
    return "set_camera_preset", {"name": name}


def _parse_template(argument: str | None) -> ParsedCommand:
    if not argument:
        raise ValueError("Использование: /template <pack>")
    name = argument.strip().lower()
    if not TEMPLATE_PACK_RE.fullmatch(name):
        raise ValueError(
            "Имя pack может содержать только a-z, 0-9, "
            "дефис и подчёркивание"
        )
    return "set_template_pack", {"name": name}


def _parse_print_queue(
    command: str,
    argument: str | None,
) -> ParsedCommand:
    """Reject arguments: print administration always covers both queues."""
    if argument is not None:
        command_name = "/" + command
        raise ValueError(f"Использование: {command_name}")
    return command, None


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
    if known_command == "set_camera_preset":
        return _parse_light(argument)
    if known_command == "set_template_pack":
        return _parse_template(argument)
    if known_command in (
        "printer_info",
        "print_queue",
        "clear_print_queue",
        "clear_photos",
        "clear_print_jobs",
    ):
        return _parse_print_queue(known_command, argument)
    if known_command:
        return known_command, None

    # /start belongs to the provider adapter.
    if command_name == "/start":
        return None

    field = command_name.removeprefix("/")
    if not CONFIG_FIELD_RE.fullmatch(field):
        return None
    if argument is None:
        if field in CAMERA_SETTING_FIELD_NAMES:
            raise ValueError(f"Использование: /{field} значение")
        return None
    command = (
        "set_camera_config"
        if field in CAMERA_SETTING_FIELD_NAMES
        else "set_app_config"
    )
    return command, {"field": field, "value": argument}


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
    if command == "set_app_config":
        return (
            f"⏳ Приложение: {data['field']} → {data['value']}; "
            "ожидаю подтверждение будки"
        )
    if command == "set_camera_preset":
        return (
            f"⏳ Пресет света: {data['name']}; "
            "ожидаю подтверждение будки"
        )
    if command == "set_template_pack":
        return (
            f"⏳ Переключаю template pack на {data['name']}; "
            "ожидаю подтверждение будки"
        )
    if command == "get_config":
        return "⏳ Запрашиваю конфиги фотобудки..."
    if command == "printer_info":
        return "⏳ Запрашиваю аппаратный счётчик DNP..."
    if command == "print_queue":
        return (
            "⏳ Запрашиваю состояние очередей Windows-принтеров..."
        )
    if command == "clear_print_queue":
        return "⏳ Очищаю очереди Windows-принтеров..."
    if command == "clear_photos":
        return "⏳ Очищаю локальную папку photos на фотобудке..."
    if command == "clear_print_jobs":
        return "⏳ Очищаю локальную папку photos_print_jobs на фотобудке..."
    return f"⏳ {command}: команда отправлена"


def failed_message(command: str, error: Exception) -> str:
    label = {
        "set_event": "Команда смены мероприятия не отправлена",
        "unblock": "Изменение блокировки не отправлено",
        "set_camera_config": "Настройка камеры не отправлена",
        "set_app_config": "Настройка приложения не отправлена",
        "set_camera_preset": "Пресет света не отправлен",
        "set_template_pack": "Template pack не переключён",
        "printer_info": "Данные DNP не запрошены",
        "print_queue": "Запрос очередей печати не отправлен",
        "clear_print_queue": "Очистка очередей печати не отправлена",
        "clear_photos": "Очистка папки photos не отправлена",
        "clear_print_jobs": "Очистка папки photos_print_jobs не отправлена",
    }.get(command, "Команда не отправлена")
    return f"❌ {label}: {error}"
