"""Cross-process MCP connect cooldown: the per-server backoff that outlives its process (#141030).

``tools.mcp_tool_discovery`` arms a geometric retry cooldown after a failed connect, but it kept
the deadline in two module dicts. Discovery runs in many short-lived processes -- a cron worker per
execution, the CLI, a one-shot, the Desktop backend -- so each one restarted the ladder at failure
count 0 and re-spawned a server that exits on start: one install logged ~98,000 identical start
banners over a few weeks, growing ``mcp-stderr.log`` to ~1 GB.

This module mirrors the deadline into ``<HERMES_HOME>/state/mcp_connect_cooldown.json`` so a fresh
process honours what the failing one published. Entries are keyed by the same connection key the
in-memory tables use (name, plus the registry scope when the process is multiplexed) and carry a
fingerprint of the server's config: editing ``command``/``args``/``env`` is a new attempt, not a
stale cooldown. Every function here is best-effort -- an unreadable, corrupt or unwritable file
degrades to "no persisted cooldown", which is exactly the old behaviour, never a failed connect.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools.mcp_tool")

#: File name under ``<HERMES_HOME>/state``; the version tag lets a later layout ignore this one.
_STATE_FILE = "mcp_connect_cooldown.json"
_STATE_VERSION = 1
#: Entries are dropped once their deadline is a week old (a much longer cooldown would be a stuck
#: state, not a backoff) and the table is capped so a huge ``mcp_servers`` map cannot grow it.
_STALE_AFTER_SEC = 7 * 24 * 3600.0
_MAX_ENTRIES = 200

#: Parsed copy of the file, keyed by its (mtime_ns, size) so a `_connect_cooldown_active` call costs
#: a stat() instead of a parse. A writer in another process changes one of the two, which reloads.
_cache: Optional[tuple[tuple[int, int], dict[str, dict[str, Any]]]] = None


def _path() -> Path:
    """The cooldown file for the profile this process serves (``HERMES_HOME`` aware)."""
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state" / _STATE_FILE


def _token(key: Any) -> str:
    """Serialize a connection key (``name`` or ``(scope, name)``) into a JSON object key."""
    return key if isinstance(key, str) else "\x1f".join(str(part) for part in key)


def _fingerprint(config: Optional[dict]) -> Optional[str]:
    """Stable digest of a server's config, or None when the caller had none to give."""
    if not config:
        return None
    try:
        blob = json.dumps(config, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _read() -> dict[str, dict[str, Any]]:
    """All published entries; ``{}`` when the file is absent, unreadable or not this layout."""
    global _cache
    path = _path()
    try:
        stat = path.stat()
    except OSError:
        _cache = None
        return {}
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _cache is not None and _cache[0] == stamp:
        return _cache[1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        logger.debug("mcp connect cooldown: ignoring unreadable %s", path, exc_info=True)
        _cache = None
        return {}
    servers = payload.get("servers") if isinstance(payload, dict) else None
    entries = {k: v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}
    _cache = (stamp, entries)
    return entries


def _write(entries: dict[str, dict[str, Any]]) -> None:
    """Publish ``entries`` atomically; any failure leaves the previous file in place."""
    global _cache
    path = _path()
    payload = {"version": _STATE_VERSION, "servers": entries}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("mcp connect cooldown: could not write %s", path, exc_info=True)
        try:
            tmp.unlink()
        except OSError:
            pass
    _cache = None


def _prune(entries: dict[str, dict[str, Any]], now: float) -> dict[str, dict[str, Any]]:
    live = {k: v for k, v in entries.items()
            if isinstance(v.get("retry_after"), (int, float)) and v["retry_after"] > now - _STALE_AFTER_SEC}
    if len(live) <= _MAX_ENTRIES:
        return live
    newest = sorted(live.items(), key=lambda item: item[1]["retry_after"], reverse=True)[:_MAX_ENTRIES]
    return dict(newest)


def _entry(key: Any, config: Optional[dict]) -> Optional[dict[str, Any]]:
    """The published entry for ``key``, when its config fingerprint still matches ``config``."""
    entry = _read().get(_token(key))
    if entry is None:
        return None
    recorded = entry.get("fingerprint")
    wanted = _fingerprint(config)
    if recorded and wanted and recorded != wanted:
        return None
    return entry


def published_failures(key: Any, config: Optional[dict] = None) -> int:
    """Consecutive failures another process recorded, so the backoff ladder continues (0 = none)."""
    entry = _entry(key, config)
    if entry is None:
        return 0
    failures = entry.get("failures")
    return failures if isinstance(failures, int) and not isinstance(failures, bool) else 0


def remaining(key: Any, config: Optional[dict] = None) -> float:
    """Seconds left on a published cooldown (0.0 when there is none, or the config changed)."""
    entry = _entry(key, config)
    if entry is None:
        return 0.0
    deadline = entry.get("retry_after")
    if not isinstance(deadline, (int, float)):
        return 0.0
    return max(0.0, float(deadline) - time.time())


def record_failure(key: Any, config: Optional[dict], failures: int, backoff_sec: float) -> None:
    """Publish a failed connect so the next process inherits the deadline instead of re-spawning."""
    now = time.time()
    entries = _prune(_read(), now)
    entries[_token(key)] = {
        "failures": failures,
        "retry_after": now + max(0.0, backoff_sec),
        "fingerprint": _fingerprint(config),
    }
    _write(entries)


def clear(key: Any) -> None:
    """Drop a published entry after a successful connect (no-op when the file has nothing for it)."""
    entries = _read()
    if _token(key) not in entries:
        return
    entries.pop(_token(key), None)
    _write(_prune(entries, time.time()))
