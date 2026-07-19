"""Photobooth VPS: Yandex.Disk media/control bridge to Telegram."""

import asyncio
import io
import json
import logging
import os
import zipfile
from pathlib import Path

import aiohttp

import yadisk_control
import yadisk_poll
import yadisk_updates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_PATH = Path(os.environ.get("VPS_CONFIG", "config_vps.json"))


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CONFIG = _load_config()
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT_ID", "")
TG_ADMIN = os.environ.get("TG_ADMIN_ID", "")
GITHUB_RELEASE_URL = os.environ.get("GITHUB_RELEASE_URL", "")
GITHUB_REPO_ZIP_URL = os.environ.get("GITHUB_REPO_ZIP_URL", "")

TG_COMMANDS = {
    "/run": "run",
    "/status": "status",
    "/logs": "send_logs",
    "/clear_logs": "clear_logs",
    "/restart": "restart",
    "/link": "link",
    "/update": "update",
    "/update_small": "update_small",
}
TG_COMMAND_KEYBOARD = {
    "inline_keyboard": [
        [{"text": text, "callback_data": text}]
        for text in TG_COMMANDS
    ],
}


async def _tg_send_text(
    session: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with session.post(f"{base}/sendMessage", json=payload) as response:
        if response.status != 200:
            log.warning(f"TG sendMessage failed: {await response.text()}")
            return False
        return True


def is_admin(user_id) -> bool:
    return bool(TG_ADMIN and user_id is not None and str(user_id) == TG_ADMIN)


async def _tg_show_keyboard(
    telegram: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
) -> None:
    await _tg_send_text(
        telegram,
        base,
        chat_id,
        "Не понял команду. Выбери действие кнопкой:",
        reply_markup=TG_COMMAND_KEYBOARD,
    )


async def _tg_answer_callback(
    telegram: aiohttp.ClientSession,
    base: str,
    callback_id: str | None,
) -> None:
    if not callback_id:
        return
    async with telegram.post(
        f"{base}/answerCallbackQuery",
        json={"callback_query_id": callback_id},
    ) as response:
        if response.status != 200:
            log.warning(f"TG answerCallbackQuery failed: {await response.text()}")


async def _do_update(small: bool = False) -> str:
    url = GITHUB_REPO_ZIP_URL if small else GITHUB_RELEASE_URL
    variable = "GITHUB_REPO_ZIP_URL" if small else "GITHUB_RELEASE_URL"
    if not url:
        raise RuntimeError(f"{variable} не задан")

    log.info(f"Downloading update from {url}")
    async with aiohttp.ClientSession() as download_session:
        async with download_session.get(
            url, timeout=aiohttp.ClientTimeout(total=300),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub вернул HTTP {response.status}")
            zip_data = await response.read()

    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as downloaded_zip:
            bad_member = downloaded_zip.testzip()
            if bad_member:
                raise ValueError(f"ZIP CRC failed: {bad_member}")
            downloaded_names = downloaded_zip.namelist()
    except zipfile.BadZipFile as exc:
        raise RuntimeError("GitHub вернул невалидный ZIP") from exc

    if small:
        skip = ("python/", "bin/", "EDSDK_Win/", ".git/")
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(zip_data)) as source, \
             zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            prefix = downloaded_names[0] if downloaded_names and downloaded_names[0].endswith("/") else ""
            for item in downloaded_names:
                relative = item[len(prefix):] if prefix else item
                if not relative or any(relative.startswith(part) for part in skip):
                    continue
                target.writestr(relative, source.read(item))
        zip_data = output.getvalue()

    with zipfile.ZipFile(io.BytesIO(zip_data)) as release_zip:
        names = [name.replace("\\", "/") for name in release_zip.namelist()]
        if "app.py" not in names:
            raise RuntimeError("ZIP не содержит app.py в корне")

    kind = "small" if small else "full"
    updates_folder = CONFIG.get("yadisk_updates_folder", "photobooth_system/updates")
    status = await yadisk_updates.publish_update(zip_data, kind, updates_folder)
    artifact = status["artifacts"][kind]
    return (
        f"✅ Обновление загружено на Диск ({kind})\n"
        f"ZIP: {len(zip_data) / 1048576:.1f} MB\n"
        f"SHA: {artifact['sha256'][:16]}\n"
        "Для установки отправь /restart"
    )


def _event_name_from_command(text: str) -> str | None:
    command, separator, argument = (text or "").strip().partition(" ")
    if command.split("@", 1)[0] != "/event":
        return None
    if not separator or not argument.strip():
        raise ValueError("Использование: /event Название события")
    return yadisk_poll.validate_event_name(argument.strip())


async def _send_disk_command(
    command: str,
    chat_id: str | int,
    data: dict | str | None = None,
) -> str:
    body = await yadisk_control.send_command(command, data, reply_chat_id=chat_id)
    return body["command_id"]


async def _tg_handle_admin_command(
    telegram: aiohttp.ClientSession,
    base: str,
    chat_id: str | int,
    text: str,
) -> None:
    text = (text or "").strip()
    try:
        event_name = _event_name_from_command(text)
    except ValueError as exc:
        await _tg_send_text(telegram, base, chat_id, f"❌ {exc}")
        return

    if event_name is not None:
        try:
            await _send_disk_command("set_event", chat_id, {"name": event_name})
            await _tg_send_text(
                telegram, base, chat_id,
                f"⏳ Переключаю event на будке и VPS: {event_name}")
        except Exception as exc:
            await _tg_send_text(telegram, base, chat_id, f"❌ Event не отправлен: {exc}")
        return

    normalized = text.split("@", 1)[0] if text.startswith("/") else text
    command = TG_COMMANDS.get(normalized)
    if not command:
        await _tg_show_keyboard(telegram, base, chat_id)
        return

    if command in ("update", "update_small"):
        label = "код" if command == "update_small" else "полный релиз"
        await _tg_send_text(telegram, base, chat_id, f"⏳ Скачиваю {label}...")
        try:
            result = await _do_update(small=command == "update_small")
        except Exception as exc:
            result = f"❌ Ошибка: {exc}"
        await _tg_send_text(telegram, base, chat_id, result)
        return

    if command == "link":
        try:
            public_url = await yadisk_poll.publish_current_folder()
            await _tg_send_text(
                telegram, base, chat_id,
                f"Event: {yadisk_poll.current_event_folder()}\n{public_url}")
        except Exception as exc:
            await _tg_send_text(telegram, base, chat_id, f"❌ Ссылка не создана: {exc}")
        return

    try:
        await _send_disk_command(command, chat_id)
        await _tg_send_text(telegram, base, chat_id, f"⏳ {command}: команда отправлена")
    except Exception as exc:
        await _tg_send_text(telegram, base, chat_id, f"❌ Команда не отправлена: {exc}")


async def tg_poll_commands() -> None:
    if not TG_TOKEN or not TG_ADMIN:
        log.warning("Telegram bot/admin is not configured")
        return
    base = f"https://api.telegram.org/bot{TG_TOKEN}"
    offset = 0
    allowed_updates = json.dumps(["message", "callback_query"])
    async with aiohttp.ClientSession() as telegram:
        while True:
            try:
                async with telegram.get(
                    f"{base}/getUpdates",
                    params={
                        "offset": offset,
                        "timeout": 10,
                        "allowed_updates": allowed_updates,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    data = await response.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if message:
                        user_id = message.get("from", {}).get("id")
                        if is_admin(user_id):
                            chat_id = message.get("chat", {}).get("id", user_id)
                            await _tg_handle_admin_command(
                                telegram, base, chat_id, message.get("text", ""))
                        continue

                    callback = update.get("callback_query")
                    if callback:
                        user_id = callback.get("from", {}).get("id")
                        if not is_admin(user_id):
                            continue
                        await _tg_answer_callback(telegram, base, callback.get("id"))
                        chat_id = callback.get("message", {}).get("chat", {}).get("id", user_id)
                        await _tg_handle_admin_command(
                            telegram, base, chat_id, callback.get("data", ""))
            except Exception as exc:
                log.warning(f"TG poll error: {exc}")
                await asyncio.sleep(5)


def _save_vps_event(name: str) -> None:
    data = _load_config()
    data["yadisk_folder"] = name
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    temporary.replace(CONFIG_PATH)
    CONFIG["yadisk_folder"] = name


async def _tg_send_log(chat_id: str | int, payload: bytes) -> bool:
    if not TG_TOKEN:
        return False
    form = aiohttp.FormData()
    form.add_field("chat_id", chat_id)
    form.add_field(
        "document", payload, filename="photobooth.log", content_type="text/plain")
    async with aiohttp.ClientSession() as telegram:
        async with telegram.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                log.warning(f"TG log send failed: {await response.text()}")
                return False
            return True


async def _handle_control_response(response: dict) -> bool:
    chat_id = response.get("reply_chat_id") or TG_ADMIN
    if not chat_id or not TG_TOKEN:
        return False

    if response["status"] == "ok" and response["command"] == "set_event":
        event_name = response.get("event_folder")
        if not event_name:
            return False
        try:
            await yadisk_poll.set_event_folder(event_name)
            _save_vps_event(event_name)
        except Exception as exc:
            log.warning(f"Control: cannot activate event on VPS: {exc}")
            return False

    artifact_path = response.get("artifact_path")
    if artifact_path:
        try:
            payload = await yadisk_control.download_bytes(artifact_path)
            if not await _tg_send_log(chat_id, payload):
                return False
        except Exception as exc:
            log.warning(f"Control: log delivery failed: {exc}")
            return False

    prefix = "✅" if response["status"] == "ok" else "❌"
    async with aiohttp.ClientSession() as telegram:
        delivered = await _tg_send_text(
            telegram,
            f"https://api.telegram.org/bot{TG_TOKEN}",
            chat_id,
            f"{prefix} {response['message']}",
        )
    if not delivered:
        return False
    if artifact_path and not await yadisk_control.delete_resource(artifact_path):
        return False
    return True


async def main() -> None:
    yadisk_folder = CONFIG.get("yadisk_folder", "")
    if not await yadisk_poll.yadisk_init(yadisk_folder, TG_TOKEN, TG_CHAT):
        log.error("Yandex.Disk media poller is not configured")
    else:
        asyncio.create_task(yadisk_poll.yadisk_poll_loop())

    control_folder = CONFIG.get("yadisk_control_folder", "photobooth_system/control")
    await yadisk_control.control_init(control_folder)
    asyncio.create_task(yadisk_control.response_poll_loop(_handle_control_response))
    asyncio.create_task(tg_poll_commands())
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
