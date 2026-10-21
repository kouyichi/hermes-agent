"""`model.base_url` relays must be probed instead of the vendor's canonical host (#121387)."""

from types import SimpleNamespace

import pytest

from hermes_cli import models


class _RecordingProfile:
    auth_type = "api_key"
    fallback_models = []

    def __init__(self):
        self.calls = []

    def fetch_models(self, api_key=None, base_url=None):
        self.calls.append((api_key, base_url))
        return ["relay-only-model"]


@pytest.mark.parametrize("provider", ["openai-codex", "copilot", "nous", "opencode-zen", "openrouter"])
def test_canonical_url_preserves_native_catalog_but_other_paths_stay_relays(monkeypatch, provider):
    from hermes_cli.config import atomic_config_write
    from hermes_constants import get_hermes_home
    from providers import get_provider_profile

    canonical = get_provider_profile(provider).base_url
    config_path = get_hermes_home() / "config.yaml"
    calls = []

    def native_catalog(slug, force_refresh):
        calls.append(slug)
        return ["native-catalog-model"]

    monkeypatch.setitem(models._PROVIDER_CATALOG_FETCHERS, provider, native_catalog)
    monkeypatch.setattr(models, "_relay_model_catalog", lambda *args: None)
    scheme, rest = canonical.split("://", 1)
    host, _, path = rest.partition("/")
    # Same route spelled differently: scheme/host case, explicit default port, trailing slash.
    shouted = f"{scheme.upper()}://{host.upper()}:443{'/' + path if path else ''}/"
    for base_url, expected in [(canonical, [provider]), (shouted, [provider]), (canonical + "/relay", [])]:
        calls.clear()
        atomic_config_write(config_path, {"model": {"provider": provider, "base_url": base_url}})
        catalog = models.provider_model_ids(provider, force_refresh=True)
        assert calls == expected
        assert ("native-catalog-model" in catalog) == bool(expected)


def test_registry_endpoint_is_canonical_when_the_profile_leaves_base_url_empty(monkeypatch):
    """TokenHub's profile leaves ``base_url`` to its auth-registry row. A ``model.base_url`` that
    ``hermes model`` saved as that endpoint is not a relay: the picker keeps the curated Hy list
    instead of replacing it with a live probe of the same host."""
    from hermes_cli.auth import PROVIDER_REGISTRY
    from providers import get_provider_profile

    assert get_provider_profile("tencent-tokenhub").base_url == ""
    endpoint = PROVIDER_REGISTRY["tencent-tokenhub"].inference_base_url
    monkeypatch.setattr(models, "_get_model_config_dict",
                        lambda: {"provider": "tencent-tokenhub", "base_url": endpoint + "/"})
    assert models._configured_relay_base_url("tencent-tokenhub") == ""
    monkeypatch.setattr(models, "_get_model_config_dict",
                        lambda: {"provider": "tencent-tokenhub", "base_url": endpoint + "/relay"})
    assert models._configured_relay_base_url("tencent-tokenhub") == endpoint + "/relay"


def test_relay_base_url_is_probed_for_configured_provider(monkeypatch):
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {
            "provider": "deepseek",
            "base_url": "http://127.0.0.1:9001/deepseek/v1",
        },
    )
    profile = _RecordingProfile()
    import providers

    monkeypatch.setattr(providers, "get_provider_profile", lambda name: profile)
    monkeypatch.setattr(models, "_api_key_credentials", lambda name: (None, None))
    monkeypatch.setattr(
        models,
        "_PROVIDER_CATALOG_FETCHERS",
        {
            "deepseek": lambda n, f: (_ for _ in ()).throw(
                AssertionError("canonical host touched")
            )
        },
    )

    assert models.provider_model_ids("deepseek", force_refresh=True) == [
        "relay-only-model"
    ]
    assert profile.calls == [(None, "http://127.0.0.1:9001/deepseek/v1")]


@pytest.mark.parametrize(
    "host", ["api.business.githubcopilot.com", "api.enterprise.githubcopilot.com"]
)
def test_a_stock_copilot_plan_host_is_not_a_relay(monkeypatch, host):
    """Copilot Business/Enterprise sit on their own stock hosts, not on api.githubcopilot.com.

    The token exchange reports ``endpoints.api`` for those accounts, so ``model.base_url``
    legitimately carries it. Treating that as a third-party relay sent the picker to the
    curated fallback and hid every model the account's live catalog lists (#142480).
    """
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {"provider": "copilot", "base_url": f"https://{host}"},
    )
    calls = []

    def native_catalog(slug, force_refresh):
        calls.append(slug)
        return ["live-account-model"]

    monkeypatch.setitem(models._PROVIDER_CATALOG_FETCHERS, "copilot", native_catalog)

    assert models._configured_relay_base_url("copilot") == ""
    assert models.provider_model_ids("copilot", force_refresh=True) == ["live-account-model"]
    assert calls == ["copilot"]


def test_a_foreign_host_for_the_same_provider_stays_a_relay(monkeypatch):
    """The stock-host rule is a host family, not "any base_url for this provider"."""
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {"provider": "copilot", "base_url": "https://copilot-proxy.example.net/v1"},
    )
    assert models._configured_relay_base_url("copilot") == "https://copilot-proxy.example.net/v1"


def test_a_path_below_a_stock_copilot_host_stays_a_relay(monkeypatch):
    """A path under the host is still a sibling route, as pinned for every canonical endpoint."""
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {"provider": "copilot", "base_url": "https://api.business.githubcopilot.com/v1"},
    )
    assert (
        models._configured_relay_base_url("copilot")
        == "https://api.business.githubcopilot.com/v1"
    )


def test_base_url_for_a_different_provider_is_ignored(monkeypatch):
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {
            "provider": "deepseek",
            "base_url": "http://127.0.0.1:9001/deepseek/v1",
        },
    )
    assert models._configured_relay_base_url("openai") == ""


@pytest.mark.parametrize(
    "probe_result",
    [
        pytest.param("raise", id="relay-hangs-or-errors"),
        pytest.param([], id="relay-404-empty-catalog"),
    ],
)
def test_a_failed_relay_probe_never_falls_back_to_the_vendor_host(monkeypatch, probe_result):
    """Egress sentinel: a configured relay is TERMINAL for live catalog egress.

    A relay that 404s, hangs or returns nothing must degrade to the LOCAL curated list.
    Falling through to the canonical fetcher would send the provider credential to exactly
    the vendor host the user routed away from, recreating #121387 on the failure path.
    """
    monkeypatch.setattr(
        models,
        "_get_model_config_dict",
        lambda: {
            "provider": "deepseek",
            "base_url": "http://127.0.0.1:9001/deepseek/v1",
        },
    )

    def _probe(**kwargs):
        if probe_result == "raise":
            raise RuntimeError("relay down")
        return probe_result

    import providers

    monkeypatch.setattr(
        providers,
        "get_provider_profile",
        lambda name: SimpleNamespace(
            auth_type="api_key", fetch_models=_probe, fallback_models=[]
        ),
    )
    monkeypatch.setattr(models, "_api_key_credentials", lambda name: (None, None))

    # Sentinels on EVERY live-egress seam below the relay probe.
    def _egress(*a, **k):
        raise AssertionError("vendor host touched after a failed relay probe")

    monkeypatch.setattr(models, "_PROVIDER_CATALOG_FETCHERS", {"deepseek": _egress})
    monkeypatch.setattr(models, "_profile_live_catalog", _egress)
    monkeypatch.setattr(models, "_merge_with_models_dev", _egress)
    monkeypatch.setattr(models, "_PROVIDER_MODELS", {"deepseek": ["curated-local"]})

    # Degrades locally to the curated list, with no vendor egress at all.
    assert models.provider_model_ids("deepseek") == ["curated-local"]
