"""Route completed sessions to independently enabled messenger adapters."""

from __future__ import annotations

from pathlib import Path

import runtime_config
import telegram_session_delivery
import vk_session_delivery


PROVIDERS = runtime_config.SESSION_DELIVERY_PROVIDERS
SessionFile = tuple[Path, str]


def enabled_providers() -> tuple[str, ...]:
    return runtime_config.archive_delivery_providers()


async def send_session(
    provider: str,
    files: list[SessionFile],
    public_url: str = "",
) -> bool:
    if provider == "telegram":
        return await telegram_session_delivery.send_session(files, public_url)
    if provider == "vk":
        return await vk_session_delivery.send_session(files, public_url)
    raise ValueError(f"unsupported session delivery provider: {provider}")
