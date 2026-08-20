"""Deliver completed photobooth media sessions to the VK archive dialog."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path

import aiohttp

import print_media
import vk_api


log = logging.getLogger(__name__)

SessionFile = tuple[Path, str]


def _content_type(path: Path, kind: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    return "video/mp4" if kind == "video" else "image/jpeg"


def _archive_peer_id() -> int | None:
    try:
        peer_id = int(vk_api.ARCHIVE_CHAT_ID)
    except (TypeError, ValueError):
        return None
    return peer_id if peer_id > 0 else None


async def _upload_file(
    session: aiohttp.ClientSession,
    peer_id: int,
    path: Path,
    kind: str,
) -> str:
    payload = await asyncio.to_thread(path.read_bytes)
    filename = path.name
    content_type = _content_type(path, kind)
    if kind == "video":
        # Reuse the application's existing community-token document upload
        # instead of adding a second authentication mode only for videos.
        return await vk_api.upload_message_document(
            session,
            peer_id,
            payload,
            filename=filename,
            content_type=content_type,
        )
    if len(payload) > print_media.MESSENGER_PHOTO_SIZE_LIMIT:
        original_size = len(payload)
        payload, quality, max_edge = await asyncio.to_thread(
            print_media.compress_jpeg,
            payload,
            max_bytes=print_media.MESSENGER_PHOTO_SIZE_LIMIT,
        )
        filename = f"{path.stem}_vk.jpg"
        content_type = "image/jpeg"
        log.info(
            "VK photo %s recompressed %.1f -> %.1f MiB "
            "(quality=%d, max_edge=%d)",
            path.name,
            original_size / 1048576,
            len(payload) / 1048576,
            quality,
            max_edge,
        )
    return await vk_api.upload_message_photo(
        session,
        peer_id,
        payload,
        filename=filename,
        content_type=content_type,
    )


async def send_session(files: list[SessionFile], public_url: str = "") -> bool:
    """Send all files to VK in groups of at most ten attachments."""
    peer_id = _archive_peer_id()
    if not vk_api.BOT_TOKEN or peer_id is None:
        log.warning("VK token/archive chat is missing")
        return False

    caption = f"Оригиналы: {public_url}" if public_url else ""
    timeout = aiohttp.ClientTimeout(total=900, connect=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for start in range(0, len(files), 10):
                attachments = []
                for path, kind in files[start:start + 10]:
                    attachments.append(
                        await _upload_file(session, peer_id, path, kind)
                    )
                await vk_api.send_attachments(
                    session,
                    peer_id,
                    attachments,
                    caption if start == 0 else "",
                )
    except Exception as exc:
        log.warning(
            "VK completed-session delivery failed: %s",
            exc,
        )
        return False
    return True
