"""A placement refusal is a cause, not a broken browser install.

On a sandboxed profile (``terminal.backend: modal`` with the default
``bot_desktop.placement: auto``) the browser is refused on purpose: its screen may not hop
out of the sandbox the user chose. The wrapper used to append "(re)install Chromium, or
switch backends" to that refusal — advice the reader cannot act on, because no browser is
missing (#143723). The refusal is typed now, so the reason and its own explicit opt-in
reach the tool result while a genuine start failure keeps the install advice.

The first test drives the real placement resolution through the real session wrapper: no
launcher runs, policy is decided before one would.
"""
from __future__ import annotations

import pytest

from tools import browser_session, browser_use_cli
from tools.bot_desktop import placement, runtime


def _refuse_modal_placement(monkeypatch) -> None:
    monkeypatch.setattr(placement, "_setting", lambda: "auto")
    monkeypatch.setattr(placement, "_terminal_backend", lambda: "modal")


def _refusal_from_real_placement(monkeypatch) -> RuntimeError:
    """The exception the spawn site raises for a refused placement (typed once the fix lands)."""
    _refuse_modal_placement(monkeypatch)
    with pytest.raises(RuntimeError) as caught:
        runtime.tool_placement()
    return caught.value


def test_a_placement_refusal_reaches_the_result_without_install_advice(monkeypatch):
    _refuse_modal_placement(monkeypatch)
    # The real wrapper: policy is resolved before any launcher or sandbox is touched.
    monkeypatch.setattr(browser_session, "get_session_info", browser_session._create_chromium_session)
    monkeypatch.setattr("tools.browser_lightpanda.using_lightpanda_engine", lambda: False)

    err = browser_use_cli._resolve_local_cdp({}, None)

    assert err is not None
    assert "cannot host a screen yet" in err  # the cause survives to the caller
    assert "placement: gateway" in err  # and the opt-in that would allow it
    assert "install Chromium" not in err  # what used to be appended to this refusal
    assert "browser.engine to auto" not in err


def test_the_lightpanda_route_reports_the_same_refusal_verbatim(monkeypatch):
    refusal = _refusal_from_real_placement(monkeypatch)
    monkeypatch.setattr("tools.browser_lightpanda.using_lightpanda_engine", lambda: True)

    def refuse(_key: str) -> dict:
        raise refusal

    monkeypatch.setattr(browser_session, "get_session_info", refuse)

    err = browser_use_cli._resolve_local_cdp({}, None)

    assert err is not None and "cannot host a screen yet" in err
    assert "browser.engine to auto" not in err  # the lightpanda route's install advice


@pytest.mark.parametrize("lightpanda", [False, True])
def test_a_genuinely_broken_local_browser_keeps_its_install_advice(monkeypatch, lightpanda):
    """Negative control: only the policy refusal drops the advice; a missing browser keeps it."""
    monkeypatch.setattr("tools.browser_lightpanda.using_lightpanda_engine", lambda: lightpanda)

    def broken(_key: str) -> dict:
        raise RuntimeError("Chromium browser is missing.")

    monkeypatch.setattr(browser_session, "get_session_info", broken)

    err = browser_use_cli._resolve_local_cdp({}, None)

    assert err is not None and "Chromium browser is missing." in err
    advice = "browser.engine to auto" if lightpanda else "install Chromium"
    assert advice in err


def test_the_refusal_stays_typed_across_the_session_wrapper(monkeypatch):
    """The wrapper prefixes context; it must not downgrade the refusal to a plain RuntimeError."""
    _refuse_modal_placement(monkeypatch)

    with pytest.raises(RuntimeError) as caught:
        browser_session._create_chromium_session("browser-exec-default")

    assert type(caught.value).__name__ == "PlacementRefused"
    assert str(caught.value).startswith("The browser cannot run here: terminal.backend is modal")


def test_the_placement_opt_in_stays_a_runtime_error(monkeypatch):
    """Subclass, not a new hierarchy: every existing ``except RuntimeError`` site keeps catching it."""
    _refuse_modal_placement(monkeypatch)
    with pytest.raises(RuntimeError):
        runtime.start()
