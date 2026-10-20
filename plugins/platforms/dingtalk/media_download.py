"""Download DingTalk inbound media so the gateway receives a LOCAL file path.

The robot file API resolves a ``download_code`` to a signed remote ``download_url``; the
adapter used to write that URL (or the bare code) straight into the message, so it ended in
``MessageEvent.media_urls``. ``gateway/session_ingress_media.py::_open_regular`` rejects
anything that is not an absolute local regular file, ``_capture_file`` re-raises that as
``RuntimeStoreError('invalid_params')``, and the whole inbound message is dropped (#141932).
Every other adapter avoids this by downloading into the media cache first — Feishu via
``cache_image_from_bytes``, Discord/Telegram likewise — so DingTalk does the same here.
"""

from __future__ import annotations

import asyncio
import logging

from gateway.platforms.base import get_inbound_media_max_bytes, validate_inbound_media_size
from gateway.platforms.media_cache import cache_media_bytes

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT_S = 30.0

# Content types that say nothing about the payload (DingTalk answers this for plain photos).
_GENERIC_CONTENT_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream"})


async def download_media_to_cache(
    url: str, *, kind: str = "document", filename: str = "", mime: str = "",
) -> str:
    """GET ``url`` and return the absolute path of its cached bytes.

    ``kind`` (``"image"``/``"audio"``/``"document"``) biases extension resolution when the
    response carries no usable ``Content-Type`` — DingTalk's file endpoint frequently answers
    ``application/octet-stream`` for a plain photo. Network/shape failures propagate so the
    caller can decide how to degrade that one attachment.
    """
    from tools.url_safety import create_ssrf_safe_async_client, is_safe_url

    if not is_safe_url(url):
        raise ValueError("Refusing unsafe inbound media URL")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)", "Accept": "*/*"}
    async with create_ssrf_safe_async_client(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            data = await _read_body_with_limit(response)
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    # DingTalk's file endpoint answers ``application/octet-stream`` for many photos; keeping it
    # would resolve the extension to ``.bin`` and hide the image from vision tools, so an unusable
    # type falls back to the empty string and ``kind`` picks the extension.
    resolved = "" if (mime or content_type).lower() in _GENERIC_CONTENT_TYPES else (mime or content_type)
    return await asyncio.to_thread(
        cache_media_bytes, data, resolved, filename_hint=filename, kind_hint=kind,
    )


async def _read_body_with_limit(response) -> bytes:
    """Buffer a streaming body under ``gateway.max_inbound_media_bytes`` (same cap as ``cache_*``)."""
    limit = get_inbound_media_max_bytes()
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        validate_inbound_media_size(total, max_bytes=limit)
        chunks.append(chunk)
    return b"".join(chunks)


def media_hint(obj, key: str) -> tuple[str, str, str]:
    """``(kind, filename, mime)`` for one download code, read from the container holding it.

    Rich-text items are dicts carrying ``type`` (``picture``/``voice``/``file``) and sometimes
    ``fileName``; ``extensions.content`` adds ``fileName`` for a ``file``/``image`` message; an
    uploaded photo arrives as ``image_content.download_code`` with no name at all.
    """
    item_type = str(obj.get("type") or "").lower() if isinstance(obj, dict) else ""
    if item_type in {"picture", "image"} or (not isinstance(obj, dict) and key == "download_code"):
        return "image", "", ""
    if item_type == "voice":
        return "audio", "", ""
    filename = (str(obj.get("fileName") or "") if isinstance(obj, dict)
                else str(getattr(obj, "file_name", "") or ""))
    if "." not in filename:
        return "document", filename, ""
    from plugins.platforms.dingtalk.inbound import EXT_MAP

    mime = EXT_MAP.get(filename.rsplit(".", 1)[-1].lower(), "")
    kind = "image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "document"
    return kind, filename, mime
