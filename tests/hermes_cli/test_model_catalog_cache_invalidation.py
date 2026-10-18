"""Post-update invalidation of the per-home model catalogs (#141101).

``provider_models_cache.json`` and ``models_dev_cache.json`` are keyed to credentials and age,
never to the installed build, so a snapshot written before an update keeps answering the picker
for the whole stale-serve window while the live probe fails — models the new build ships stay
invisible while their removal makes them appear instantly. Contracts under test:

- the per-home helper drops both payloads plus the ETag sidecar, is idempotent, and is a no-op
  for a home that never wrote them;
- the boot/update tail step drops the ACTIVE home's copies (bundled/sealed updates never run
  ``hermes update``'s tail);
- ``hermes update`` sweeps the active home AND every sibling profile (one shared checkout);
- behaviourally: a stale row answers the picker before the drop and cannot answer it after.
"""

from __future__ import annotations

import json
import time

from hermes_cli.models_cache_invalidation import (
    CACHE_FILES,
    invalidate_for_home,
)


def _seed(home):
    (home / "provider_models_cache.json").write_text("{}", encoding="utf-8")
    (home / "models_dev_cache.json").write_text("{}", encoding="utf-8")
    (home / "models_dev_cache.etag").write_text('"abc"', encoding="utf-8")


def _survivors(home):
    return [name for name in CACHE_FILES if (home / name).exists()]


def test_helper_drops_payloads_and_etag(tmp_path):
    _seed(tmp_path)

    assert set(invalidate_for_home(tmp_path)) == {
        "provider_models_cache.json",
        "models_dev_cache.json",
        "models_dev_cache.etag",
    }
    assert _survivors(tmp_path) == []
    assert invalidate_for_home(tmp_path) == []  # idempotent, and safe on a home that never cached


def test_boot_step_drops_the_active_homes_catalogs(tmp_path, monkeypatch):
    from hermes_cli import post_update

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed(tmp_path)

    assert ("drop_model_catalog_caches", post_update.step_drop_model_catalog_caches) in (
        post_update.BOOT_HOME_STEPS
    )
    result = post_update.step_drop_model_catalog_caches()
    assert result["ok"] is True
    assert set(result["dropped"]) == {
        "provider_models_cache.json",
        "models_dev_cache.json",
        "models_dev_cache.etag",
    }
    assert _survivors(tmp_path) == []
    assert post_update.step_drop_model_catalog_caches()["ok"] is True  # absent caches are fine


def test_update_invalidates_the_catalogs_for_every_profile(tmp_path, monkeypatch):
    """The checkout is shared, so one profile's update changes every profile's catalog truth."""
    from hermes_cli import backup as _backup
    from hermes_cli import update_cmd
    from hermes_cli import update_cmd_maint

    root = tmp_path / "home"
    alpha = root / "profiles" / "alpha"
    beta = root / "profiles" / "beta"
    for home in (root, alpha, beta):
        home.mkdir(parents=True, exist_ok=True)
        _seed(home)

    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: root)
    monkeypatch.setattr(
        _backup, "_sibling_profile_homes", lambda _home: [("alpha", alpha), ("beta", beta)]
    )

    update_cmd_maint._invalidate_model_catalog_caches()

    assert _survivors(root) == []
    assert _survivors(alpha) == []
    assert _survivors(beta) == []


def test_stale_row_stops_answering_the_picker_after_the_drop(tmp_path, monkeypatch):
    from hermes_cli import models

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(models, "_credential_fingerprint", lambda provider: "fp")
    monkeypatch.setattr(models, "provider_model_ids", lambda *args, **kwargs: [])
    # Two hours old: past the 1 h TTL, inside the 7-day stale-serve window — the shape #141101
    # reports, where a failed live probe keeps the pre-update catalog in charge.
    (tmp_path / "provider_models_cache.json").write_text(
        json.dumps(
            {"openai": {"fp": "fp", "at": time.time() - 7200, "models": ["gpt-pre-update"]}}
        ),
        encoding="utf-8",
    )

    assert models.cached_provider_model_ids("openai", non_blocking=True) == ["gpt-pre-update"]

    invalidate_for_home(tmp_path)

    assert models.cached_provider_model_ids("openai", non_blocking=True) == []
