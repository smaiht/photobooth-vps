"""Route outbound messages through the transport selected by ReplyTarget."""

from __future__ import annotations

import aiohttp

from messaging import ReplyTarget
import telegram_api
import vk_api

Document = tuple[bytes, str, str]


def _vk_peer_id(target: ReplyTarget) -> int:
    try:
        peer_id = int(target.conversation_id)
    except ValueError as exc:
        raise ValueError("VK conversation_id must be an integer") from exc
    if peer_id <= 0:
        raise ValueError("VK conversation_id must be positive")
    return peer_id


async def send_text(target: ReplyTarget, text: str) -> bool:
    target = ReplyTarget.from_value(target)
    async with aiohttp.ClientSession() as session:
        if target.provider == "telegram":
            return await telegram_api.send_text(
                session,
                telegram_api.BOT_API_BASE,
                target.conversation_id,
                text,
            )
        if target.provider == "vk":
            message_id = await vk_api.send_text(
                session,
                _vk_peer_id(target),
                text,
            )
            return message_id is not None
    raise ValueError(f"unsupported reply provider: {target.provider}")


async def send_photo(
    target: ReplyTarget,
    image: bytes,
    caption: str,
    *,
    filename: str = "image.png",
    content_type: str = "image/png",
    keyboard: dict | None = None,
    parse_mode: str | None = None,
) -> bool:
    target = ReplyTarget.from_value(target)
    async with aiohttp.ClientSession() as session:
        if target.provider == "telegram":
            message_id = await telegram_api.send_photo(
                session,
                telegram_api.BOT_API_BASE,
                target.conversation_id,
                image,
                caption,
                keyboard,
                None,
                filename=filename,
                content_type=content_type,
                parse_mode=parse_mode,
            )
            return message_id is not None
        if target.provider == "vk":
            message_id = await vk_api.send_photo(
                session,
                _vk_peer_id(target),
                image,
                caption,
                filename=filename,
                content_type=content_type,
                keyboard=keyboard,
            )
            return message_id is not None
    raise ValueError(f"unsupported reply provider: {target.provider}")


async def send_document(
    target: ReplyTarget,
    payload: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
) -> bool:
    target = ReplyTarget.from_value(target)
    if target.provider == "telegram":
        return await telegram_api.send_document(
            target.conversation_id,
            payload,
            filename,
            content_type,
        )
    if target.provider == "vk":
        async with aiohttp.ClientSession() as session:
            message_id = await vk_api.send_document(
                session,
                _vk_peer_id(target),
                payload,
                filename,
                content_type,
            )
        return message_id is not None
    raise ValueError(f"unsupported reply provider: {target.provider}")


async def send_documents(
    target: ReplyTarget,
    documents: list[Document],
) -> bool:
    target = ReplyTarget.from_value(target)
    if not documents:
        raise ValueError("at least one document is required")
    if len(documents) == 1:
        payload, filename, content_type = documents[0]
        return await send_document(target, payload, filename, content_type)
    if target.provider == "telegram":
        return await telegram_api.send_documents(
            target.conversation_id,
            documents,
        )
    if target.provider == "vk":
        async with aiohttp.ClientSession() as session:
            message_id = await vk_api.send_documents(
                session,
                _vk_peer_id(target),
                documents,
            )
        return message_id is not None
    raise ValueError(f"unsupported reply provider: {target.provider}")
