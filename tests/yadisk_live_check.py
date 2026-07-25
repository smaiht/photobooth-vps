"""Isolated Yandex.Disk API lifecycle check.

Run with YADISK_TOKEN in the process environment. The script creates a unique
temporary folder, never lists or modifies existing folders, and removes the
temporary folder in a finally block. It never prints the token.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


API = "https://cloud-api.yandex.net/v1/disk"
TOKEN = os.environ.get("YADISK_TOKEN", "").strip()
TIMEOUT = 60


class ApiError(RuntimeError):
    pass


def request(method: str, url: str, *, params: dict | None = None,
            data: bytes | None = None, auth: bool = True,
            allowed: tuple[int, ...] = (200,)) -> tuple[int, bytes]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": "photobooth-yadisk-live-check/1"}
    if auth:
        headers["Authorization"] = f"OAuth {TOKEN}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if exc.code in allowed:
            return exc.code, body
        detail = body.decode("utf-8", errors="replace")[:500]
        raise ApiError(f"{method} {url}: HTTP {exc.code}: {detail}") from exc
    if status not in allowed:
        raise ApiError(f"{method} {url}: unexpected HTTP {status}")
    return status, body


def json_request(method: str, path: str, *, params: dict | None = None,
                 data: bytes | None = None, allowed: tuple[int, ...] = (200,)) -> tuple[int, dict]:
    status, body = request(method, f"{API}{path}", params=params, data=data,
                           allowed=allowed)
    return status, json.loads(body) if body else {}


def wait_operation(href: str) -> None:
    for _ in range(60):
        _, body = request("GET", href, allowed=(200,))
        status = json.loads(body).get("status")
        if status == "success":
            return
        if status == "failed":
            raise ApiError(f"operation failed: {href}")
        time.sleep(1)
    raise ApiError(f"operation timeout: {href}")


def wait_resource(path: str, expected: bytes) -> dict:
    expected_md5 = hashlib.md5(expected).hexdigest()
    for _ in range(30):
        try:
            _, meta = json_request(
                "GET", "/resources",
                params={"path": path, "fields": "name,path,size,md5,sha256,type"},
            )
            if meta.get("size") == len(expected) and meta.get("md5") == expected_md5:
                return meta
        except ApiError:
            pass
        time.sleep(1)
    raise ApiError(f"resource did not become verifiable: {path}")


def create_directory(path: str) -> None:
    status, _ = json_request(
        "PUT", "/resources", params={"path": path}, data=b"",
        allowed=(201, 409),
    )
    if status not in (201, 409):
        raise ApiError(f"directory creation failed: {path}")


def upload(path: str, payload: bytes) -> dict:
    _, link = json_request(
        "GET", "/resources/upload",
        params={"path": path, "overwrite": "true"},
    )
    status, _ = request(
        "PUT", link["href"], data=payload, auth=False, allowed=(201, 202),
    )
    print(f"upload {path.rsplit('/', 1)[-1]}: HTTP {status}")
    return wait_resource(path, payload)


def download(path: str) -> bytes:
    _, link = json_request("GET", "/resources/download", params={"path": path})
    _, body = request("GET", link["href"], auth=False, allowed=(200,))
    return body


def delete(path: str) -> None:
    status, result = json_request(
        "DELETE", "/resources",
        params={"path": path, "permanently": "true"},
        data=b"", allowed=(202, 204),
    )
    if status == 202:
        wait_operation(result["href"])


def finish_async(status: int, result: dict) -> None:
    if status == 202 and result.get("href"):
        wait_operation(result["href"])


def main() -> int:
    if not TOKEN:
        print("YADISK_TOKEN is required", file=sys.stderr)
        return 2

    suffix = secrets.token_hex(4)
    folder = f"/photobooth_api_test_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{suffix}"
    control = f"{folder}/control"
    inbox = f"{control}/to_vps"
    published = False

    # A valid 1x1 JPEG plus a small ISO-BMFF header for transport checks.
    photo = base64.b64decode(
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
        "2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
        "wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9k="
    )
    video = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"

    try:
        _, disk = json_request(
            "GET", "", params={"fields": "total_space,used_space,max_file_size"})
        free = int(disk.get("total_space", 0)) - int(disk.get("used_space", 0))
        print(f"oauth: OK; free={free} bytes; max_file={disk.get('max_file_size')} bytes")

        for path in (folder, control, inbox):
            create_directory(path)
        print(f"temporary folder created: {folder}")

        media = {
            f"{folder}/20260717_120000_livecheck_photo_01.jpg": photo,
            f"{folder}/20260717_120000_livecheck_video.mp4": video,
        }
        metadata = {path: upload(path, payload) for path, payload in media.items()}

        files = [
            {
                "name": path.rsplit("/", 1)[-1],
                "kind": "video" if path.endswith(".mp4") else "photo",
                "size": len(media[path]),
                "md5": metadata[path]["md5"],
            }
            for path in media
        ]
        manifests = []
        for index in (1, 2):
            manifest = json.dumps({
                "schema_version": 2,
                "message_type": "session_ready",
                "event_folder": folder.lstrip("/"),
                "session_id": f"livecheck{index}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": files,
            }, separators=(",", ":")).encode("utf-8")
            manifest_path = f"{inbox}/session_livecheck{index}.json"
            upload(manifest_path, manifest)
            manifests.append((manifest_path, manifest))

        _, first_page = json_request(
            "GET", "/resources",
            params={
                "path": inbox, "limit": 1, "offset": 0, "sort": "name",
                "fields": "_embedded.total,_embedded.limit,_embedded.offset,_embedded.items.name",
            },
        )
        _, second_page = json_request(
            "GET", "/resources",
            params={
                "path": inbox, "limit": 1, "offset": 1, "sort": "name",
                "fields": "_embedded.total,_embedded.limit,_embedded.offset,_embedded.items.name",
            },
        )
        first = first_page["_embedded"]
        second = second_page["_embedded"]
        names = [first["items"][0]["name"], second["items"][0]["name"]]
        if first["total"] != 2 or len(set(names)) != 2:
            raise ApiError(f"pagination mismatch: total={first['total']}, names={names}")
        print(f"pagination: OK; total={first['total']}; pages={names}")

        if download(next(iter(media))) != next(iter(media.values())):
            raise ApiError("media download content mismatch")
        if download(manifests[0][0]) != manifests[0][1]:
            raise ApiError("manifest download content mismatch")
        print("download + content verification: OK")

        for source, _ in manifests:
            delete(source)
            request(
                "GET",
                f"{API}/resources",
                params={"path": source, "fields": "path,type"},
                allowed=(404,),
            )
        print("manifest inbox deletion: OK")

        status, result = json_request(
            "PUT", "/resources/publish", params={"path": folder}, data=b"",
            allowed=(200, 201, 202),
        )
        finish_async(status, result)
        published = True
        public_meta = None
        for _ in range(20):
            _, public_meta = json_request(
                "GET", "/resources",
                params={"path": folder, "fields": "name,public_key,public_url"},
            )
            if public_meta.get("public_key") and public_meta.get("public_url"):
                break
            time.sleep(1)
        if not public_meta or not public_meta.get("public_key"):
            raise ApiError("published folder has no public key")
        _, public_view = request(
            "GET", f"{API}/public/resources",
            params={"public_key": public_meta["public_key"], "limit": 1},
            auth=False, allowed=(200,),
        )
        if json.loads(public_view).get("name") != folder.rsplit("/", 1)[-1]:
            raise ApiError("public resource lookup mismatch")
        print("publish + anonymous public read: OK")

        status, result = json_request(
            "PUT", "/resources/unpublish", params={"path": folder}, data=b"",
            allowed=(200, 201, 202),
        )
        finish_async(status, result)
        published = False
        print("unpublish: OK")
        print("Yandex.Disk live check: PASS")
        return 0
    finally:
        if published:
            try:
                status, result = json_request(
                    "PUT", "/resources/unpublish", params={"path": folder}, data=b"",
                    allowed=(200, 201, 202, 404),
                )
                finish_async(status, result)
            except Exception as exc:
                print(f"cleanup unpublish warning: {exc}", file=sys.stderr)
        try:
            status, result = json_request(
                "DELETE", "/resources",
                params={"path": folder, "permanently": "true", "force_async": "true"},
                allowed=(202, 204, 404),
            )
            finish_async(status, result)
            print("temporary folder cleanup: OK")
        except Exception as exc:
            print(f"CRITICAL cleanup warning for {folder}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
