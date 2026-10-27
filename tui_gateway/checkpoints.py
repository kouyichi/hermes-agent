"""Checkpoint-enablement resolution for TUI-created agents (issue #79625).

Desktop sessions (``hermes serve`` → TUI gateway) never set ``HERMES_TUI_CHECKPOINTS``
(that env var is only set by the ``hermes --tui --checkpoints`` CLI path), so agent
creation previously gated filesystem checkpointing on the env var alone and silently
ignored ``checkpoints.enabled`` in config.yaml. These helpers resolve enablement the
way the messaging gateway does (``gateway/run.py`` ``_checkpoint_agent_kwargs``):
the env var (explicit CLI opt-in) wins when set, otherwise the ``checkpoints``
config section applies with ``DEFAULT_CONFIG`` defaults.
"""

import os

from utils import is_truthy_value


def _checkpoint_section(cfg: dict | None) -> dict:
    """The ``checkpoints`` config section, normalised: the legacy ``checkpoints: true`` bool form
    becomes ``{"enabled": ...}``; an absent or malformed section becomes ``{}`` so each lookup
    falls back to ``DEFAULT_CONFIG``."""
    cp_cfg = (cfg or {}).get("checkpoints", {})
    if isinstance(cp_cfg, bool):
        return {"enabled": cp_cfg}
    return cp_cfg if isinstance(cp_cfg, dict) else {}


def _load_checkpoints_enabled(cfg: dict | None = None) -> bool:
    """Whether filesystem checkpoints are enabled for TUI-created agents.

    Keeps the legacy ``checkpoints: true`` bool form working and falls back to the
    ``DEFAULT_CONFIG`` default when the section is absent or malformed.
    """
    from hermes_cli.config import DEFAULT_CONFIG
    return bool(_checkpoint_section(cfg).get("enabled", DEFAULT_CONFIG["checkpoints"]["enabled"]))


def resolve_checkpoint_limits(cfg: dict | None = None) -> dict:
    """The three store limits ``AIAgent`` takes (``checkpoint_max_*``), resolved from the profile's
    ``checkpoints:`` section with ``DEFAULT_CONFIG`` defaults.

    Desktop/TUI agents were built with ``checkpoints_enabled`` alone, so a store over
    ``checkpoints.max_total_size_mb`` ran the over-cap round on every checkpoint and the config
    keys were unreadable there (#145268). Same precedence as the CLI
    (``hermes_cli/cli_init_mixin.py``) and the messaging gateway
    (``gateway/session_worker_construct.py::checkpoint_agent_kwargs``) — the parity test in
    ``tests/tui_gateway/test_checkpoints_config_fallback.py`` keeps the three in step.
    """
    from hermes_cli.config import DEFAULT_CONFIG
    section, defaults = _checkpoint_section(cfg), DEFAULT_CONFIG["checkpoints"]
    return {"checkpoint_max_snapshots": section.get("max_snapshots", defaults["max_snapshots"]),
            "checkpoint_max_total_size_mb": section.get("max_total_size_mb", defaults["max_total_size_mb"]),
            "checkpoint_max_file_size_mb": section.get("max_file_size_mb", defaults["max_file_size_mb"])}


def resolve_checkpoints_enabled(cfg: dict | None = None) -> bool:
    """Resolve checkpoint enablement with the env-var override first.

    ``HERMES_TUI_CHECKPOINTS`` (set by ``hermes --tui --checkpoints``) is the
    explicit opt-in and wins when present; otherwise fall back to the
    ``checkpoints`` config section (issue #79625 — the desktop backend never
    sets the env var, so config was being silently ignored).
    """
    env_val = os.environ.get("HERMES_TUI_CHECKPOINTS")
    if env_val is not None:
        return is_truthy_value(env_val)
    return _load_checkpoints_enabled(cfg)


def _resolve_checkpoint_hash(mgr, cwd: str, ref: str) -> str:
    """``/rollback N`` style number → the checkpoint's hash; a real hash passes through."""
    try:
        checkpoints = mgr.list_checkpoints(cwd)
        idx = int(ref) - 1
    except ValueError:
        return ref
    if 0 <= idx < len(checkpoints):
        return checkpoints[idx].get("hash", ref)
    raise ValueError(f"Invalid checkpoint number. Use 1-{len(checkpoints)}.")
