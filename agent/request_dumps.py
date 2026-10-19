"""The provider-error request dump: whether it is written, and what its ``error`` block holds.

``dump_api_request_debug`` fires on every non-retryable provider error and captures the whole
request body — the prompt included — under ``<hermes home>/sessions/request_dump_*.json``. That
copy sits outside the ``state.db`` retention policy until its session row is pruned, so
``logging.request_dumps: false`` is the switch for deployments that keep prompts only in
``state.db`` under ``sessions.retention_days``.

The gate defaults to ON, and an unreadable config keeps it on: a broken configuration must never
silently drop the diagnostics a bug report needs.
"""

from __future__ import annotations

import logging
from typing import Any

from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _ra():
    """Lazy ``run_agent`` reference for test-patch routing."""
    import run_agent
    return run_agent


def request_dumps_enabled() -> bool:
    """Whether a provider-error request dump may be written (``logging.request_dumps``).

    Read per dump rather than cached at import: a dump is written only on a hard provider error,
    and ``load_config_readonly`` answers from the process's config cache, so the repeated read is
    cheap. Anything unreadable keeps the historical default (on).
    """
    try:
        from hermes_cli.config import load_config_readonly
        logging_cfg = (load_config_readonly() or {}).get("logging", {})
    except Exception:  # a config parse must never decide "off" — keep the dump, log why
        logger.warning(
            "Could not read logging.request_dumps; request dumps stay enabled", exc_info=True)
        return True
    if not isinstance(logging_cfg, dict):
        return True
    return is_truthy_value(logging_cfg.get("request_dumps"), default=True)


def api_error_debug_info(error: Exception) -> dict[str, Any]:
    """The dump's ``error`` block: type/message plus whatever the SDK error carries."""
    info: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
    info.update({
        k: v for k in ("status_code", "request_id", "code", "param", "type", "body")
        if (v := getattr(error, k, None)) is not None
    })
    response_obj = getattr(error, "response", None)
    if response_obj is not None:
        try:
            info["response_status"] = getattr(response_obj, "status_code", None)
            info["response_text"] = response_obj.text
        except Exception as e:  # health: allow BLE001 -- response_obj can be any SDK object; .text may read a stream
            _ra().logger.debug("Could not extract error response details: %s", e, exc_info=True)
    return info
