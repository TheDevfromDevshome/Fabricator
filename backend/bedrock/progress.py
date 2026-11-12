"""Per-server Bedrock crossplay progress store.

Mirrors ``backend.server.install_progress`` deliberately: same merge semantics,
same terminal-phase vocabulary, same in-memory lifecycle. Kept separate so
crossplay polling cannot disturb the installer progress the upgrade/modpack UI
already watches.

Phase vocabulary:

    starting          — worker thread spawned
    downloading       — Geyser/Floodgate artefacts being fetched
    generating_config — server started once so Geyser can emit config.yml
    creating_tunnel   — playit NetherNet tunnel being created / token awaited
    patching_config   — Geyser config.yml being rewritten
    deleting_tunnel   — crossplay disabled; tunnel cleanup in flight
    done              — setup/teardown completed
    failed            — entry carries 'error': <str>
"""
from __future__ import annotations

import threading
from typing import Any, Dict

from backend.utils.time import iso_z_now


_lock = threading.Lock()
_store: Dict[str, Dict[str, Any]] = {}

_TERMINAL_PHASES = frozenset({"done", "failed"})


def update(server_id: str, /, **kwargs: Any) -> None:
    """Merge ``kwargs`` into the entry for ``server_id``."""
    timestamp = iso_z_now()
    with _lock:
        existing = _store.get(server_id) or {}
        _store[server_id] = {**existing, **kwargs, "updated_at": timestamp}


def get(server_id: str, /) -> Dict[str, Any]:
    with _lock:
        return dict(_store.get(server_id) or {})


def clear(server_id: str, /) -> None:
    with _lock:
        _store.pop(server_id, None)


def is_active(server_id: str, /) -> bool:
    with _lock:
        entry = _store.get(server_id)
        if not entry:
            return False
        phase = entry.get("phase")
        return bool(phase) and phase not in _TERMINAL_PHASES
