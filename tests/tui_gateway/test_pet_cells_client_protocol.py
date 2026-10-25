"""``pet.cells`` must ask the CLIENT which graphics protocol its terminal speaks (#144619).

The gateway owner is a systemd user service, so it has no ``TERM``/``TERM_PROGRAM``:
``agent.pet.render.detect_terminal_graphics()`` answers ``unicode`` there for every
terminal, and ``display.pet.render_mode: auto`` could never reach native pixels for a
gateway-connected TUI even though the Ink client runs inside a kitty-capable terminal.
The client is the only process that owns the terminal, so it now reports its own
answer and the owner trusts it for ``auto`` (an explicit ``render_mode`` still wins).

A new module rather than an append: ``tests/tui_gateway/test_tui_gateway_server.py``
is already over its ``FILE_LINES`` target, so any growth there fails ``scripts/check``.
"""

from __future__ import annotations

import pytest

from agent.pet import render, store
from tui_gateway import server


class _FakePet:
    slug = "sabrina"
    display_name = "Sabrina"
    exists = True
    spritesheet = "/tmp/sabrina.webp"


def _install_pet(monkeypatch) -> list[str]:
    """PetRenderer construction modes, with the daemon-shaped (TERM-less) environment.

    Every ``pet.cells`` branch builds exactly one ``PetRenderer``, so the recorded
    ``mode`` names the branch that answered: ``kitty`` (native pixels) or ``unicode``
    (half-block mosaic). A fixture would shadow its own name in the test signatures,
    which the health gate reports as F811.
    """
    modes: list[str] = []

    class _Renderer:
        def __init__(self, _spritesheet, *, mode, scale, unicode_cols=0):
            modes.append(mode)

        def frame_count(self, state: str) -> int:
            return 1

        def cells(self, state: str, index: int, *, cols: int):
            return [[((0, 0, 0, 255), (0, 0, 0, 255))]]

        def kitty_payload(self, state: str, *, image_id: int) -> dict:
            return {"cols": 4, "rows": 2, "placeholder": ["x" * 4], "frames": ["\x1b_Gm=1\x1b\\"]}

    for name in ("TERM", "TERM_PROGRAM", "KITTY_WINDOW_ID", "ITERM_SESSION_ID", "WEZTERM_PANE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(store, "resolve_active_pet", lambda slug: _FakePet())
    monkeypatch.setattr(render, "PetRenderer", _Renderer)
    return modes


def _display_cfg(monkeypatch, render_mode: str) -> None:
    monkeypatch.setattr(server, "_pet_display_cfg", lambda: {
        "enabled": True, "slug": "sabrina", "render_mode": render_mode, "scale": 0.33,
    })


def _cells(params: dict) -> dict:
    response = server.handle_request({"id": "1", "method": "pet.cells", "params": params})
    assert "result" in response, response
    return response["result"]


def test_client_protocol_reaches_native_pixels_in_auto_mode(monkeypatch):
    """A gateway-connected client whose terminal speaks kitty gets the kitty payload."""
    modes = _install_pet(monkeypatch)
    _display_cfg(monkeypatch, "auto")

    result = _cells({"graphics": True, "state": "idle", "protocol": "kitty"})

    assert result["graphics"] == "kitty"
    assert result["imageId"] == render.kitty_image_id(_FakePet.slug)
    assert result["placeholder"] == ["x" * 4]
    assert modes == ["kitty"]


def test_client_protocol_wins_over_the_owners_own_environment(monkeypatch):
    """A client that reports half-blocks must not receive kitty escapes (xterm.js, tmux)."""
    modes = _install_pet(monkeypatch)
    _display_cfg(monkeypatch, "auto")
    monkeypatch.setenv("TERM", "xterm-kitty")  # the daemon's env says kitty: the client knows better

    result = _cells({"graphics": True, "state": "idle", "protocol": "unicode"})

    assert "graphics" not in result
    assert isinstance(result["frames"][0][0][0], list)
    assert modes == ["unicode"]


def test_absent_protocol_keeps_the_daemon_side_detection(monkeypatch):
    """Older clients send no ``protocol``: env detection stays the fallback."""
    modes = _install_pet(monkeypatch)
    _display_cfg(monkeypatch, "auto")
    monkeypatch.setenv("TERM", "xterm-kitty")

    result = _cells({"graphics": True, "state": "idle"})

    assert result["graphics"] == "kitty"
    assert modes == ["kitty"]


def test_explicit_render_mode_beats_the_client_protocol(monkeypatch):
    """``display.pet.render_mode`` pinned by the operator is never overruled by the client."""
    modes = _install_pet(monkeypatch)
    _display_cfg(monkeypatch, "unicode")

    result = _cells({"graphics": True, "state": "idle", "protocol": "kitty"})

    assert "graphics" not in result
    assert modes == ["unicode"]
