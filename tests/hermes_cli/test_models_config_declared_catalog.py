"""A ``providers:``-declared model catalog must survive ``--refresh`` (#141694).

``provider_model_ids`` is the shared "best known catalog" helper, and
``cached_provider_model_ids`` only caches non-empty results. A BYOK provider declared in
``config.yaml`` under ``providers:`` has no ``_PROVIDER_MODELS`` row and no plugin profile, so the
static tail answered ``[]``: ``hermes model --refresh`` wiped ``provider_models_cache.json`` and the
rebuild had nothing to store, leaving every surface that reads that helper with a model-less
provider. These tests pin the declared ``models:`` block as that provider's catalog.

The picker rows that read config.yaml directly (``model_switch_providers``) are deliberately out of
scope: they already union the endpoint's live listing on top of the declared block.
"""

from __future__ import annotations

import pytest

from hermes_cli import models
from hermes_cli.models_config_providers import declared_provider_models


_RIVER = {
    "api": "https://api.river.ai/v1",
    "key_env": "RIVER_API_KEY",
    "transport": "chat_completions",
    "default_model": "deepseek-ai/DeepSeek-V4.1-Flash",
    "models": {
        "zai-org/GLM-5.3-Flash": {},
        "deepseek-ai/DeepSeek-V4.1-Flash": {},
    },
}


@pytest.fixture
def config_providers(monkeypatch):
    """Serve a synthetic ``config.yaml`` ``providers:`` block to every config reader."""

    def _install(providers):
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {"providers": providers},
        )

    return _install


def test_provider_model_ids_answers_the_declared_catalog(config_providers):
    """The static tail is empty for a config-declared provider, so it must read ``models:``."""
    config_providers({"river": _RIVER})

    assert models.provider_model_ids("river") == [
        "zai-org/GLM-5.3-Flash",
        "deepseek-ai/DeepSeek-V4.1-Flash",
    ]


def test_declared_models_accept_the_list_and_row_forms(config_providers):
    """``models:`` is a mapping in the docs, but the legacy list/``[{id: ...}]`` forms are config too."""
    config_providers({
        "listform": {"api": "https://list.example/v1", "models": ["m-one", "m-two"]},
        "rowform": {"api": "https://row.example/v1", "models": [{"id": "r-one"}, {"name": "r-two"}]},
    })

    assert models.provider_model_ids("listform") == ["m-one", "m-two"]
    assert models.provider_model_ids("rowform") == ["r-one", "r-two"]


def test_refresh_rebuilds_the_config_declared_cache_row(config_providers):
    """The half that made ``--refresh`` destructive: a wipe nothing could rebuild."""
    config_providers({"river": _RIVER})
    models.clear_provider_models_cache()

    assert models._load_provider_models_cache() == {}
    assert models.cached_provider_model_ids("river", force_refresh=True) == [
        "zai-org/GLM-5.3-Flash",
        "deepseek-ai/DeepSeek-V4.1-Flash",
    ]
    assert models._load_provider_models_cache()["river"]["models"] == [
        "zai-org/GLM-5.3-Flash",
        "deepseek-ai/DeepSeek-V4.1-Flash",
    ]


def test_disabled_entry_declares_nothing(config_providers):
    config_providers({"river": {**_RIVER, "enabled": False}})

    assert declared_provider_models("river") is None
    assert models.provider_model_ids("river") == []


def test_undeclared_and_blank_names_answer_nothing(config_providers):
    config_providers({"river": _RIVER})

    assert declared_provider_models("nobody") is None
    assert declared_provider_models("") is None
    assert models.provider_model_ids("nobody") == []


def test_a_curated_builtin_keeps_its_own_catalog(config_providers):
    """A back-compat ``providers.deepseek`` block must not shadow the curated list."""
    config_providers({"deepseek": {"api": "https://relay.example/v1", "models": ["shadow-only"]}})

    catalog = models.provider_model_ids("deepseek")

    assert catalog
    assert "shadow-only" not in catalog
