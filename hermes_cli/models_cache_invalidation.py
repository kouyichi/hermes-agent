"""Drop per-home model catalog snapshots after the installed code changes.

``provider_models_cache.json`` and ``models_dev_cache.json`` are keyed to credentials and
wall-clock age, never to the installed build. A row written before an update therefore keeps
answering the picker for the whole stale-serve window (7 days in
:data:`hermes_cli.models._PROVIDER_MODELS_STALE_SERVE_MAX`) whenever the live probe fails, so
models the new build ships stay invisible even though the running code knows them (#141101).
The update tail and the boot step drop both per home, exactly like the live plugin catalog's
snapshot (#119340).

``models_dev_cache.etag`` goes with the payload it vouches for: an ``If-None-Match`` without a
servable cache earns a 304 and leaves the process with no registry at all.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROVIDER_MODELS_CACHE = "provider_models_cache.json"
MODELS_DEV_CACHE = "models_dev_cache.json"
MODELS_DEV_ETAG = "models_dev_cache.etag"

# The ETag sidecar is not a catalog of its own, but it must never outlive the payload it vouches for.
CACHE_FILES: tuple[str, ...] = (PROVIDER_MODELS_CACHE, MODELS_DEV_CACHE, MODELS_DEV_ETAG)


def invalidate_for_home(home: Path | str) -> list[str]:
    """Delete *home*'s model catalog snapshots; return the names actually dropped.

    Best-effort per file: an unwritable home must not fail an update or a boot, and a home that
    never wrote the caches is a no-op. The provider cache is dropped under its own cross-process
    write lock so a concurrent row writer cannot save its pre-delete snapshot back afterwards.
    """
    home_path = Path(home)
    dropped: list[str] = []
    for name in CACHE_FILES:
        path = home_path / name
        try:
            if not path.exists():
                continue
            if name == PROVIDER_MODELS_CACHE:
                from hermes_cli.models_cache_lock import write_under_cache_lock

                def _drop(target: Path = path) -> None:
                    target.unlink(missing_ok=True)

                write_under_cache_lock(path, _drop)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Model catalog cache: could not drop %s: %s", path, exc)
            continue
        # The lock helper swallows its own failures; report only a drop that landed.
        if not path.exists():
            dropped.append(name)
    return dropped
