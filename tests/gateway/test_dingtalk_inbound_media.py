"""DingTalk inbound media must reach the gateway as a LOCAL file path (#141932).

``gateway/session_ingress_media.py::_open_regular`` accepts only an absolute local
regular file and raises ``ValueError('native media must be a local regular file')``
for anything else; ``_capture_file`` re-raises that as
``RuntimeStoreError('invalid_params')``, which drops the WHOLE inbound message. Every
other adapter hands ``MessageEvent.media_urls`` a downloaded cache path, but the
DingTalk adapter wrote the resolved remote ``download_url`` (or the bare download code)
back into the download code, so every inbound image / voice note / file failed
admission and never reached the agent.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig


@pytest.fixture(autouse=True)
def _fake_dingtalk_models(monkeypatch):
    """Stand-ins for the optional alibabacloud SDK models (absent in this test env)."""
    import plugins.platforms.dingtalk.adapter as dt

    def factory(**kwargs):
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(dt, "tea_util_models", SimpleNamespace(RuntimeOptions=factory), raising=False)
    monkeypatch.setattr(
        dt,
        "dingtalk_robot_models",
        SimpleNamespace(RobotMessageFileDownloadRequest=factory,
                        RobotMessageFileDownloadHeaders=factory),
        raising=False,
    )


@pytest.fixture
def adapter(monkeypatch):
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    for key in ("DINGTALK_ALLOWED_USERS", "DINGTALK_REQUIRE_MENTION", "DINGTALK_FREE_RESPONSE_CHATS"):
        monkeypatch.delenv(key, raising=False)
    instance = DingTalkAdapter(PlatformConfig(enabled=True, extra={}))
    instance._stream_client = SimpleNamespace(get_access_token=lambda: "token")
    instance._robot_sdk = SimpleNamespace(
        robot_message_file_download_with_options_async=AsyncMock(
            return_value=SimpleNamespace(body=SimpleNamespace(download_url="https://cdn.dingtalk.com/a.png"))
        )
    )
    return instance


def _recorder(monkeypatch, cached_path, *, error=None):
    """Replace the module's downloader with a recorder that returns ``cached_path``."""
    import plugins.platforms.dingtalk.adapter as dt

    calls = []

    async def _fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if error is not None:
            raise error
        return str(cached_path)

    monkeypatch.setattr(dt, "download_media_to_cache", _fake)
    return calls


def _image_message(code: str):
    return SimpleNamespace(
        text=None,
        image_content=SimpleNamespace(download_code=code),
        rich_text_content=None,
        rich_text=None,
        message_type="picture",
    )


def _rich_text_message(items, *, sdk_shape: bool = True):
    """Rich-text message in the >= 0.20 (``rich_text_content``) or legacy ``rich_text`` shape."""
    content = SimpleNamespace(rich_text_list=items) if sdk_shape else None
    return SimpleNamespace(
        text=None,
        image_content=None,
        rich_text_content=content,
        rich_text=None if sdk_shape else items,
        message_type="richText",
    )


def _media_urls(message):
    from plugins.platforms.dingtalk.adapter import DingTalkAdapter

    return DingTalkAdapter._extract_media(DingTalkAdapter, message)[1]


@pytest.mark.asyncio
async def test_resolved_image_code_becomes_a_local_cache_path(adapter, monkeypatch, tmp_path):
    """The resolved URL must be downloaded; ``media_urls`` carries the cache path."""
    cached = tmp_path / "img_abc.png"
    cached.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 24)
    calls = _recorder(monkeypatch, cached)
    message = _image_message("dl_image_code")

    await adapter._resolve_media_codes(message)

    assert message.image_content.download_code == str(cached)
    assert calls == [{"url": "https://cdn.dingtalk.com/a.png", "kind": "image", "filename": "", "mime": ""}]
    assert _media_urls(message) == [str(cached)]


@pytest.mark.asyncio
@pytest.mark.parametrize("sdk_shape", [True, False], ids=["rich_text_content", "legacy-rich_text"])
async def test_rich_text_document_keeps_its_filename(adapter, monkeypatch, tmp_path, sdk_shape):
    cached = tmp_path / "report.pdf"
    cached.write_bytes(b"%PDF-1.4 test")
    calls = _recorder(monkeypatch, cached)
    message = _rich_text_message(
        [{"type": "file", "fileName": "report.pdf", "downloadCode": "dl_doc"}], sdk_shape=sdk_shape,
    )

    await adapter._resolve_media_codes(message)

    assert calls == [{"url": "https://cdn.dingtalk.com/a.png", "kind": "document",
                      "filename": "report.pdf", "mime": "application/pdf"}]
    assert _media_urls(message) == [str(cached)]


@pytest.mark.asyncio
async def test_failed_download_drops_the_attachment_not_the_message(adapter, monkeypatch, tmp_path):
    """A failed fetch must not leave a URL that kills the whole turn."""
    _recorder(monkeypatch, tmp_path / "unused", error=OSError("boom"))
    message = _image_message("dl_image_code")

    await adapter._resolve_media_codes(message)

    assert not message.image_content.download_code
    assert _media_urls(message) == []


@pytest.mark.asyncio
async def test_missing_download_url_does_not_write_a_remote_url(adapter):
    """``response.body`` without ``download_url`` used to leave the bare code in place."""
    adapter._robot_sdk.robot_message_file_download_with_options_async = AsyncMock(
        return_value=SimpleNamespace(body=SimpleNamespace(download_url=None))
    )
    message = _image_message("dl_image_code")

    await adapter._resolve_media_codes(message)

    assert not message.image_content.download_code
    assert _media_urls(message) == []


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict):
        self._body, self.headers = body, headers

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        yield self._body


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, response, seen):
        self._response, self.seen = response, seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        self.seen.append((method, url, headers))
        return _FakeStream(self._response)


def _patch_client(monkeypatch, body: bytes, headers: dict):
    seen = []
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: True)
    monkeypatch.setattr(
        "tools.url_safety.create_ssrf_safe_async_client",
        lambda **kwargs: _FakeClient(_FakeResponse(body, headers), seen),
    )
    return seen


@pytest.mark.asyncio
async def test_download_media_to_cache_writes_the_bytes(monkeypatch):
    """The downloader caches real bytes (octet-stream image still resolves to .jpg)."""
    import plugins.platforms.dingtalk.media_download as md

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    seen = _patch_client(monkeypatch, png, {"content-type": "application/octet-stream"})

    path = await md.download_media_to_cache("https://cdn.dingtalk.com/a.png", kind="image")

    assert Path(path).is_absolute()
    assert Path(path).read_bytes() == png
    assert path.endswith(".jpg")
    assert seen and seen[0][0] == "GET"


@pytest.mark.asyncio
async def test_download_media_to_cache_enforces_the_inbound_cap(monkeypatch):
    import plugins.platforms.dingtalk.media_download as md

    _patch_client(monkeypatch, b"x" * 64, {"content-type": "image/png"})
    monkeypatch.setattr(md, "get_inbound_media_max_bytes", lambda: 16)

    with pytest.raises(ValueError):
        await md.download_media_to_cache("https://cdn.dingtalk.com/a.png", kind="image")


@pytest.mark.asyncio
async def test_download_media_to_cache_refuses_unsafe_urls(monkeypatch):
    import plugins.platforms.dingtalk.media_download as md

    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda url: False)

    with pytest.raises(ValueError):
        await md.download_media_to_cache("http://169.254.169.254/latest/meta-data/", kind="image")


@pytest.mark.asyncio
async def test_cached_path_satisfies_the_gateway_admission_contract(monkeypatch):
    """End to end: the cached path is admitted, the remote URL is what the capturer rejects (#141932)."""
    import plugins.platforms.dingtalk.media_download as md
    from gateway.session_ingress_media import capture_native_media
    from hermes_state_runtime import RuntimeStoreError

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    _patch_client(monkeypatch, png, {"content-type": "image/png"})
    path = await md.download_media_to_cache("https://cdn.dingtalk.com/a.png", kind="image")

    references = capture_native_media([path])
    assert references[0]["size"] == len(png)

    with pytest.raises(RuntimeStoreError):
        capture_native_media(["https://cdn.dingtalk.com/a.png"])
