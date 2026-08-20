"""Small async client for the Kie image-edit API."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import aiohttp


KIE_API_BASE = "https://api.kie.ai"
KIE_UPLOAD_URL = "https://kieai.redpandaai.co/api/file-stream-upload"
KIE_MODEL = "gpt-image-2-image-to-image"
KIE_IMAGE_RESOLUTION = "1K"
KIE_INPUT_MAX_BYTES = 10 * 1024 * 1024
KIE_TASK_POLL_SECONDS = 10
KIE_TASK_TIMEOUT_SECONDS = 15 * 60

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=10)
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=10)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120, connect=10)
_PENDING_STATES = frozenset({"waiting", "queuing", "generating"})


class KieApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        result_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.result_url = result_url


def _api_key() -> str:
    key = os.environ.get("KIE_API_KEY", "").strip()
    if not key:
        raise KieApiError("KIE_API_KEY не настроен")
    return key


def _safe_message(payload: object) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return "неизвестная ошибка"


def _retryable_status(status: object) -> bool:
    return (
        isinstance(status, int)
        and not isinstance(status, bool)
        and (status in {408, 429, 455} or status >= 500)
    )


async def _request_json(
    method: str,
    url: str,
    operation: str,
    *,
    timeout: aiohttp.ClientTimeout = _REQUEST_TIMEOUT,
    retry_not_found: bool = False,
    **kwargs,
) -> dict:
    headers = {"Authorization": f"Bearer {_api_key()}"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.request(
                method,
                url,
                timeout=timeout,
                **kwargs,
            ) as response:
                status = response.status
                raw = await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise KieApiError(
            f"Kie {operation}: не удалось подключиться",
            retryable=True,
        ) from exc

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise KieApiError(
            f"Kie {operation}: некорректный JSON",
            retryable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise KieApiError(
            f"Kie {operation}: неожиданный ответ",
            retryable=True,
        )
    if status != 200:
        raise KieApiError(
            f"Kie {operation}: HTTP {status}, {_safe_message(payload)}",
            retryable=(retry_not_found and status == 404)
            or _retryable_status(status),
        )
    return payload


def _response_data(payload: dict, operation: str) -> dict:
    code = payload.get("code")
    if code != 200:
        raise KieApiError(
            f"Kie {operation}: код {code}, {_safe_message(payload)}",
            retryable=isinstance(code, int) and _retryable_status(code),
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise KieApiError(
            f"Kie {operation}: ответ не содержит data",
            retryable=True,
        )
    return data


async def upload_image(
    payload: bytes,
    *,
    filename: str,
) -> str:
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("empty Kie upload")
    if len(payload) > KIE_INPUT_MAX_BYTES:
        raise ValueError("Kie input exceeds 10 MB")
    form = aiohttp.FormData()
    form.add_field(
        "file",
        payload,
        filename=filename,
        content_type="application/octet-stream",
    )
    form.add_field("uploadPath", "images/photobooth")
    form.add_field("fileName", filename)
    response = await _request_json(
        "POST",
        KIE_UPLOAD_URL,
        "upload",
        timeout=_UPLOAD_TIMEOUT,
        data=form,
    )
    if response.get("success") is not True or response.get("code") != 200:
        raise KieApiError(
            f"Kie upload: {_safe_message(response)}",
            retryable=_retryable_status(response.get("code", 0)),
        )
    data = response.get("data")
    if not isinstance(data, dict):
        raise KieApiError("Kie upload: ответ не содержит data", retryable=True)
    url = data.get("downloadUrl") or data.get("fileUrl")
    if not _http_url(url):
        raise KieApiError("Kie upload: ответ не содержит URL", retryable=True)
    return url


async def create_image_task(
    *,
    prompt: str,
    input_url: str,
    aspect_ratio: str,
) -> str:
    if aspect_ratio not in {"3:2", "2:3"}:
        raise ValueError("Kie aspect ratio must be 3:2 or 2:3")
    response = await _request_json(
        "POST",
        f"{KIE_API_BASE}/api/v1/jobs/createTask",
        "createTask",
        json={
            "model": KIE_MODEL,
            "input": {
                "prompt": prompt,
                "input_urls": [input_url],
                "aspect_ratio": aspect_ratio,
                "resolution": KIE_IMAGE_RESOLUTION,
            },
        },
    )
    data = _response_data(response, "createTask")
    task_id = data.get("taskId")
    if not isinstance(task_id, str) or not task_id.strip():
        raise KieApiError(
            "Kie createTask: ответ не содержит taskId",
            retryable=True,
        )
    return task_id.strip()


async def get_task_details(task_id: str) -> dict:
    response = await _request_json(
        "GET",
        f"{KIE_API_BASE}/api/v1/jobs/recordInfo",
        "recordInfo",
        retry_not_found=True,
        params={"taskId": task_id},
    )
    return _response_data(response, "recordInfo")


def task_result_url(details: dict) -> str | None:
    state = details.get("state")
    if state in _PENDING_STATES:
        return None
    if state == "fail":
        reason = details.get("failMsg") or details.get("failCode") or "ошибка генерации"
        raise KieApiError(f"Kie generation failed: {str(reason)[:300]}")
    if state != "success":
        raise KieApiError(f"Kie recordInfo: неизвестный статус {state!r}")

    result = details.get("resultJson")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError as exc:
            raise KieApiError(
                "Kie recordInfo: некорректный resultJson",
                retryable=True,
            ) from exc
    urls = result.get("resultUrls") if isinstance(result, dict) else None
    if not isinstance(urls, list):
        raise KieApiError(
            "Kie recordInfo: результат не содержит resultUrls",
            retryable=True,
        )
    for url in urls:
        if _http_url(url):
            return url
    raise KieApiError(
        "Kie recordInfo: результат не содержит URL изображения",
        retryable=True,
    )


def _http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def download_result(url: str, *, max_bytes: int) -> bytes:
    if not _http_url(url):
        raise ValueError("invalid Kie result URL")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=_DOWNLOAD_TIMEOUT) as response:
                if response.status != 200:
                    raise KieApiError(
                        f"Kie result download: HTTP {response.status}",
                        retryable=_retryable_status(response.status),
                    )
                if response.content_length and response.content_length > max_bytes:
                    raise KieApiError("Kie result is too large")
                payload = await response.content.read(max_bytes + 1)
    except KieApiError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise KieApiError(
            "Kie result download: не удалось подключиться",
            retryable=True,
        ) from exc
    if not payload:
        raise KieApiError("Kie result download: пустой файл", retryable=True)
    if len(payload) > max_bytes:
        raise KieApiError("Kie result is too large")
    return payload
