"""Small shared retry policy for outbound messenger transports."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping


MAX_ATTEMPTS = 3
RETRY_DELAYS = (0.5, 1.5)
MAX_RETRY_AFTER = 30.0
_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


def retryable_http_status(status: int) -> bool:
    return status in _RETRYABLE_HTTP_STATUSES or 500 <= status <= 599


def retry_after_seconds(
    headers: Mapping[str, str] | None,
    body: bytes | None = None,
) -> float | None:
    value: object | None = None
    if headers is not None:
        value = headers.get("Retry-After")
    if value is None and body:
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            parameters = payload.get("parameters")
            if isinstance(parameters, dict):
                value = parameters.get("retry_after")
    try:
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER)


async def wait_before_retry(
    failed_attempt: int,
    *,
    retry_after: float | None = None,
) -> None:
    if retry_after is not None:
        delay = retry_after
    else:
        index = min(max(failed_attempt - 1, 0), len(RETRY_DELAYS) - 1)
        delay = RETRY_DELAYS[index]
    await asyncio.sleep(delay)
