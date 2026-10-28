"""#146226: an explicit Anthropic base URL must not hide a pooled ``api_key`` credential.

Resuming a CLI session restores the route stored on its row, and that row's ``base_url`` is the
provider's OWN endpoint (``https://api.anthropic.com``) — no user override. The runtime ladder still
takes the explicit ``--base-url`` rung for it, and that rung asked ``resolve_anthropic_token`` alone.
The resolver reads env keys and OAuth grants only (by design: it is the read-only token resolver), so
an install whose only Anthropic credential is an ``api_key`` entry written by
``hermes auth add anthropic`` failed the first turn of every resumed session with "No Anthropic
credentials found", while a fresh session on the same provider worked (it resolves through the
credential pool instead).

The tests pin the rung's credential order — explicit key, then the documented env/OAuth resolution,
then a pooled ``api_key`` entry — and that a pooled OAuth entry is still left to the token resolver,
which owns refresh/rotation.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import runtime_provider as rp
from hermes_cli.auth import AuthError
from hermes_cli.cli_model_switch_mixin import stored_session_route

ANTHROPIC_ENDPOINT = "https://api.anthropic.com"


class _Pool:
    """Minimal ``CredentialPool`` stand-in: one entry, records the models it was asked to select for."""

    def __init__(self, entry):
        self._entry = entry
        self.selected: list = []

    def has_credentials(self) -> bool:
        return self._entry is not None

    def select(self, model=None):
        self.selected.append(model)
        return self._entry


def _pool_entry(*, auth_type: str = "api_key", token: str = "sk-ant-pooled"):
    return SimpleNamespace(auth_type=auth_type, access_token=token, runtime_api_key=None,
                           base_url=ANTHROPIC_ENDPOINT, runtime_base_url=None)


@pytest.fixture
def no_env_token(monkeypatch):
    """No ``ANTHROPIC_*`` key and no OAuth grant resolves (the reporter's install)."""
    monkeypatch.setattr("agent.anthropic_credentials.resolve_anthropic_token", lambda model=None: None)


@pytest.fixture
def pool(monkeypatch):
    """Install a pool holding *one* entry and return it (``agent.credential_pool.load_pool`` shimmed
    onto ``hermes_cli.runtime_provider``'s module-level name, which is what the rung reads)."""
    def _install(entry):
        fake = _Pool(entry)
        monkeypatch.setattr(rp, "load_pool", lambda provider: fake)
        return fake
    return _install


@pytest.mark.parametrize("base_url", [ANTHROPIC_ENDPOINT, "https://relay.example/anthropic"])
def test_explicit_base_url_resolves_the_pooled_api_key(no_env_token, pool, base_url):
    """A base-URL-only override finds the pooled key — canonical host or a user endpoint alike."""
    pool(_pool_entry())

    runtime = rp.resolve_runtime_provider(requested="anthropic", explicit_base_url=base_url,
                                         target_model="claude-opus-5-5")

    assert runtime["api_key"] == "sk-ant-pooled"
    assert runtime["api_mode"] == "anthropic_messages"
    assert runtime["base_url"] == base_url


def test_a_restored_session_route_resolves_through_the_pool(no_env_token, pool):
    """End to end on the reported surface: the stored route's base URL resolves a credential."""
    pool(_pool_entry())
    row = {"model": "claude-opus-5-5", "model_config": {"gateway_runtime": {
        "provider": "anthropic", "base_url": ANTHROPIC_ENDPOINT, "api_mode": "anthropic_messages"}}}

    route = stored_session_route(row, current_model="another-max", current_provider="custom:another")
    assert route is not None
    model, provider, base_url, api_mode, provider_changed = route
    assert (model, provider, base_url, api_mode, provider_changed) == (
        "claude-opus-5-5", "anthropic", ANTHROPIC_ENDPOINT, "anthropic_messages", True)

    runtime = rp.resolve_runtime_provider(requested=provider, explicit_base_url=base_url, target_model=model)

    assert runtime["api_key"] == "sk-ant-pooled"


def test_env_token_still_wins_over_a_pooled_api_key(monkeypatch, pool):
    """The documented resolution order is not reversed: a resolvable env/OAuth token is used as is."""
    monkeypatch.setattr("agent.anthropic_credentials.resolve_anthropic_token", lambda model=None: "env-token")
    fake = pool(_pool_entry())

    runtime = rp.resolve_runtime_provider(requested="anthropic", explicit_base_url=ANTHROPIC_ENDPOINT)

    assert runtime["api_key"] == "env-token"
    assert fake.selected == []  # the pool is not even consulted when the token resolver answers


def test_a_pooled_oauth_entry_is_left_to_the_token_resolver(no_env_token, pool):
    """OAuth grants keep one owner: a pool OAuth row is not taken raw by the explicit rung."""
    pool(_pool_entry(auth_type="oauth"))

    with pytest.raises(AuthError):
        rp.resolve_runtime_provider(requested="anthropic", explicit_base_url=ANTHROPIC_ENDPOINT)


def test_explicit_api_key_still_wins_over_the_pool(no_env_token, pool):
    """``--api-key`` is the user's choice and keeps outranking every stored credential."""
    fake = pool(_pool_entry())

    runtime = rp.resolve_runtime_provider(requested="anthropic", explicit_api_key="sk-ant-flag",
                                          explicit_base_url=ANTHROPIC_ENDPOINT)

    assert runtime["api_key"] == "sk-ant-flag"
    assert fake.selected == []
