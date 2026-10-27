"""Config values parsed into an ``AIAgent`` at construction time.

``GatewayRunner`` caches one agent per session and decides reuse from the config
signature in ``gateway/run_agent_cache.py``. Every value listed here is read while
the agent is built and frozen for the life of that cached agent, so a change to
one MUST invalidate the cache — otherwise a mid-gateway edit is silently ignored.

The table lives here rather than on the facade (``gateway/run.py``) because that
file is at its code-health line cap; ``GatewayRunner._CACHE_BUSTING_CONFIG_KEYS``
re-exports it so every reader keeps its name.
"""

from __future__ import annotations

CACHE_BUSTING_CONFIG_KEYS: tuple = (
    ("model", "context_length"), ("compression", "enabled"),
    ("compression", "progress_notices"), ("compression", "threshold"),
    ("compression", "model_thresholds"), ("compression", "threshold_tokens"),
    ("compression", "codex_gpt55_autoraise"), ("compression", "codex_app_server_auto"),
    ("compression", "codex_responses_native"),
    ("compression", "codex_responses_compact_threshold"), ("compression", "in_place"),
    ("compression", "checkpoint_required"), ("compression", "micro_compact"),
    ("compression", "micro_compact_every_n_turns"),
    ("compression", "micro_compact_defrag_threshold_tokens"), ("compression", "target_ratio"),
    ("compression", "tail_mode"), ("compression", "protect_last_n"),
    ("compression", "proactive_prune_tokens"),
    ("compression", "proactive_prune_min_result_chars"),
    ("compression", "proactive_prune_min_reclaim_tokens"),
    ("compression", "min_tail_user_messages"), ("agent", "disabled_toolsets"),
    ("memory", "provider"),
    # Frozen into the built-in store / flags by ``agent_init._init_memory``: editing a memory
    # limit has to rebuild the cached agent, or the session keeps writing against the caps it
    # started with and the memory tool reports the stale one (#145284).
    ("memory", "memory_char_limit"), ("memory", "user_char_limit"),
    ("memory", "nudge_interval"), ("memory", "memory_enabled"),
    ("memory", "user_profile_enabled"),
    ("checkpoints", "enabled"), ("checkpoints", "max_snapshots"),
    ("checkpoints", "max_total_size_mb"), ("checkpoints", "max_file_size_mb"))
