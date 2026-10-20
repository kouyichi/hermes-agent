"""The Gemini native adapter must carry the media parts ``video_analyze`` sends.

``tools.vision_tools._media_messages`` builds a video request as
``{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}``. The
adapter's OpenAI-content translation only recognised ``image_url``, so the part was
dropped and Gemini received a text-only request: the model asked the user to attach the
video while the tool reported ``success: true`` (#141763).

Scope: the media part that reaches ``generateContent``. Whether the tool should still
answer ``success: true`` when a provider route cannot carry video at all is a separate
contract and is not asserted here.
"""

from __future__ import annotations

import base64

from agent.gemini_native_adapter import _build_gemini_contents
from tools.vision_tools import _media_messages

_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"hermes-video-payload"
_MP4_DATA_URL = "data:video/mp4;base64," + base64.b64encode(_MP4_BYTES).decode("ascii")
_PROMPT = "What happens in this clip?"


def _user_parts(content) -> list[dict]:
    contents, _system = _build_gemini_contents([{"role": "user", "content": content}])
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    return contents[0]["parts"]


def _inline_parts(parts: list[dict]) -> list[dict]:
    return [part["inlineData"] for part in parts if "inlineData" in part]


def test_video_analyze_message_reaches_gemini_as_inline_data():
    messages = _media_messages(_PROMPT, "video_url", _MP4_DATA_URL)

    parts = _user_parts(messages[0]["content"])

    inline = _inline_parts(parts)
    assert len(inline) == 1, f"the video part was dropped: {parts}"
    assert inline[0]["mimeType"] == "video/mp4"
    assert base64.b64decode(inline[0]["data"]) == _MP4_BYTES
    assert parts[0] == {"text": _PROMPT}, "the prompt must stay the turn's first part"


def test_image_message_still_reaches_gemini_as_inline_data():
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
    messages = _media_messages(_PROMPT, "image_url", data_url)

    inline = _inline_parts(_user_parts(messages[0]["content"]))

    assert len(inline) == 1
    assert inline[0]["mimeType"] == "image/png"


def test_a_video_part_without_an_inline_data_url_is_not_invented():
    content = [
        {"type": "text", "text": _PROMPT},
        {"type": "video_url", "video_url": {"url": "https://example.com/clip.mp4"}},
    ]

    parts = _user_parts(content)

    assert parts == [{"text": _PROMPT}], "a remote video URL is not an inlineData part"
