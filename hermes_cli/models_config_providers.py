"""The catalog a config-declared provider declares, for providers with no curated row.

``providers:`` entries in ``config.yaml`` are the documented way to add an OpenAI-compatible
(BYOK) provider. Such a provider has no ``_PROVIDER_MODELS`` row and no plugin profile, so
``models.provider_model_ids``' static tail answered ``[]`` for it: the picker rows that read
config directly still worked, but every surface sharing the "best known catalog" helper — and
``provider_models_cache.json``, which only caches non-empty results — saw a model-less provider.
``hermes model --refresh`` made that visible by wiping the cache (``clear_provider_models_cache``
removes the whole file) because the rebuild then had nothing to store (#141694).

Seeding the declared ``models:`` block into the static tail keeps that catalog non-empty without
an extra network probe: the declared list is the provider's own authoritative declaration, and
surfaces that can reach the endpoint (the picker rows, the desktop catalog) still union its live
``/models`` listing. A provider that also has a curated row keeps it — the caller only asks here
when ``_PROVIDER_MODELS`` has nothing, so a back-compat declaration for a built-in provider can
never shadow the curated list.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def declared_provider_models(provider: str) -> Optional[list[str]]:
    """Model ids declared by ``providers.<provider>.models``, or ``None`` when it declares none.

    Keys are matched by string identity (an unquoted YAML key loads as an int), a disabled entry
    declares nothing, and both the mapping form and the legacy list / ``[{id: ...}]`` forms are
    accepted. Any config read failure answers ``None``: the catalog tail falls back to its
    previous (empty) answer rather than raising into every picker surface.
    """
    name = str(provider or "").strip()
    if not name:
        return None
    try:
        from hermes_cli.config import is_provider_enabled, load_config_readonly
        from hermes_cli.config_providers import _normalize_provider_models, find_provider_entry

        _stored, entry = find_provider_entry((load_config_readonly() or {}).get("providers"), name)
        if not isinstance(entry, dict) or not is_provider_enabled(entry):
            return None
        models, _discovered = _normalize_provider_models(entry.get("models"))
        return list(models) or None
    except Exception:
        logger.debug("declared provider catalog unavailable for %s", name, exc_info=True)
        return None
