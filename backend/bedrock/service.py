"""Orchestration for Bedrock crossplay.

One toggle means four jobs, run in a worker thread while the per-server lock
serializes them against start/stop/install:

    1. put Geyser + Floodgate in ``plugins/`` (GeyserMC API)
    2. create/reuse a playit.gg NetherNet tunnel and a RakNet tunnel, each
       forwarding to its own local UDP port
    3. patch ``plugins/Geyser-Spigot/config.yml`` into ``transport: both`` with
       the NetherNet token and both ports

NetherNet is the modern Bedrock transport (needs playit's NXS signaling);
RakNet is the classic one and needs no token. Serving both means a client can
pick either address. The worker owns the long-running parts; the route only
validates and returns a snapshot. Setup and teardown both require a stopped
server, which removes the start/stop races that would otherwise leave Geyser
with a half-written config.
"""
from __future__ import annotations

import logging
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from backend.managed import is_managed
from backend.playit import agent as playit_agent
from backend.server import storage
from backend.server.installer import loader_content_kind
from backend.server.locks import get_server_lock
from backend.server.registry import get_server_process_registry
from backend.utils import platform as platform_utils

from . import config_patch, geyser, playit_tunnel, progress

logger = logging.getLogger(__name__)

# Stored on the server record. Names are intentionally explicit so a reader of
# servers.json can tell what wrote them.
STATE_FLAG = "bedrockCrossplay"
PORT_KEY = "bedrockPort"  # RakNet UDP port (Geyser ``bedrock.port``)
NETHER_PORT_KEY = "bedrockNetherPort"  # NetherNet UDP port (``webrtc-port``)
TUNNEL_KEY = "bedrockTunnelId"  # NetherNet tunnel
RAKNET_TUNNEL_KEY = "bedrockRaknetTunnelId"
ADDRESS_KEY = "bedrockAddress"  # NetherNet host
RAKNET_ADDRESS_KEY = "bedrockRaknetAddress"  # RakNet host:port
ERROR_KEY = "bedrockError"

# NetherNet clients always dial :19132; RakNet uses whatever port playit
# allocates. Both transports need their own local UDP port, hence two ports per
# server inside the agent's 19132–19232 range.
CLIENT_PORT = 19132
PORT_MIN = 19132
PORT_MAX = 19232
PORT_RANGE = range(PORT_MIN, PORT_MAX + 1)
PORTS_PER_SERVER = 2

# Transport labels shown in the UI. NetherNet/RakNet are product names, so they
# stay untranslated — the surrounding sentence carries the localisation.
TRANSPORT_LABELS = {
    playit_tunnel.NETHERNET: "NetherNet",
    playit_tunnel.RAKNET: "RakNet",
}

_CONFIG_TIMEOUT = 240.0
_CONFIG_POLL_INTERVAL = 1.0
_AGENT_START_TIMEOUT = 40.0
_AGENT_POLL_INTERVAL = 1.0

_REASON_MESSAGES = {
    "managed": "Bedrock crossplay is not available in managed hosting.",
    "unsupported_platform": "playit.gg Bedrock tunnels are only supported on Linux.",
    "unsupported_loader": (
        "Bedrock crossplay needs a plugin server (Paper, Purpur, Folia or Pufferfish)."
    ),
    "not_installed": "Install the server before enabling Bedrock crossplay.",
    "installing": "The server is still installing. Wait for it to finish.",
}

_EXPECTED_ERRORS = (
    config_patch.ConfigPatchError,
    geyser.GeyserError,
    playit_tunnel.PlayitTunnelError,
)


class BedrockError(RuntimeError):
    """A user-actionable refusal from the synchronous validation path."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# Small seams (monkeypatched in tests)
# --------------------------------------------------------------------------- #

def _registry():
    return get_server_process_registry()


def _config_path(plugins_dir: str | Path) -> Path:
    return Path(plugins_dir) / "Geyser-Spigot" / "config.yml"


def _provider_state_dir(plugins_dir: str | Path) -> Path:
    return Path(plugins_dir) / "Geyser-Spigot" / "nethernet" / "provider-state"


def _clear_provider_state(plugins_dir: str | Path) -> None:
    """Drop Geyser's cached NetherNet signaling registration.

    ``provider-state/provider-state.json`` is keyed by the signaling endpoint,
    not by the tunnel token. Geyser therefore reuses the *previous* registration
    after a token change and keeps announcing the old public address. Removing
    the directory makes the next start register the tunnel we just created.

    Best effort: no directory is the normal first-run case, and a deletion
    problem must not abort an otherwise successful enable.
    """
    state_dir = _provider_state_dir(plugins_dir)
    if not state_dir.exists():
        return
    try:
        shutil.rmtree(state_dir)
    except OSError as exc:
        logger.warning(
            "bedrock: could not clear Geyser provider state at %s: %s",
            state_dir,
            exc,
        )
        return
    logger.info(
        "bedrock: cleared Geyser provider state so it re-registers with the current tunnel"
    )


# --------------------------------------------------------------------------- #
# Support / state
# --------------------------------------------------------------------------- #

def support_reason(server: dict) -> Optional[str]:
    """Why crossplay cannot be enabled, or ``None`` when it can."""
    if is_managed():
        return "managed"
    if not platform_utils.is_linux():
        return "unsupported_platform"

    loader = str(server.get("loader") or "").strip().lower()
    if loader_content_kind(loader) != "plugin":
        return "unsupported_loader"

    status = str(server.get("status") or "").strip().lower()
    if status == "installing":
        return "installing"
    if status in ("pending", "failed"):
        return "not_installed"
    return None


def get_state(server: dict) -> dict:
    """Snapshot consumed by the settings toggle, the overview row and playit tab."""
    server_id = str(server.get("id") or "")
    reason = support_reason(server)
    entry = progress.get(server_id)
    playit_status, playit_ready = _playit_state()

    host = server.get(ADDRESS_KEY)
    raknet = _normalise_address(server.get(RAKNET_ADDRESS_KEY))
    error = entry.get("error") or server.get(ERROR_KEY)
    active = progress.is_active(server_id)
    running = _server_running(server_id)

    return {
        "server_id": server_id,
        "supported": reason is None,
        "reason": reason,
        "reason_message": _REASON_MESSAGES.get(reason) if reason else None,
        "enabled": bool(server.get(STATE_FLAG)),
        "active": active,
        "phase": entry.get("phase"),
        "plugin": entry.get("plugin"),
        "plugin_version": entry.get("plugin_version"),
        "error": error,
        "bedrock_port": _as_int(server.get(PORT_KEY)),
        "nether_port": _as_int(server.get(NETHER_PORT_KEY)),
        "client_port": CLIENT_PORT,
        "tunnel_id": server.get(TUNNEL_KEY),
        "raknet_tunnel_id": server.get(RAKNET_TUNNEL_KEY),
        "address_host": host or None,
        "address": display_address(host),
        "raknet_address": raknet,
        "addresses": bedrock_addresses(server),
        "server_running": running,
        "playit_status": playit_status,
        "playit_ready": playit_ready,
        "manageable": reason is None and not active and not running,
    }


def bedrock_addresses(server: dict) -> list[dict]:
    """``[{type,label,address}]`` — one entry per transport once it is ready."""
    entries: list[dict] = []
    nether = display_address(server.get(ADDRESS_KEY))
    if nether:
        entries.append({
            "type": playit_tunnel.NETHERNET,
            "label": TRANSPORT_LABELS[playit_tunnel.NETHERNET],
            "address": nether,
        })
    raknet = _normalise_address(server.get(RAKNET_ADDRESS_KEY))
    if raknet:
        entries.append({
            "type": playit_tunnel.RAKNET,
            "label": TRANSPORT_LABELS[playit_tunnel.RAKNET],
            "address": raknet,
        })
    return entries


def _normalise_address(value: object) -> Optional[str]:
    """Strip whitespace from a stored host:port, or ``None`` when empty."""
    if not value:
        return None
    text = str(value).strip()
    return text or None


def display_address(host: Optional[str]) -> Optional[str]:
    """Client-facing address. NetherNet clients always connect on :19132."""
    if not host:
        return None
    host = str(host).strip()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[1:end]
        return f"[{host}]:{CLIENT_PORT}" if ":" in host else f"{host}:{CLIENT_PORT}"
    # A single colon is a host:port pair; two or more is a bare IPv6 literal.
    if host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    elif ":" in host:
        return f"[{host}]:{CLIENT_PORT}"
    return f"{host}:{CLIENT_PORT}"


# --------------------------------------------------------------------------- #
# Enable / disable (synchronous validation, worker does the work)
# --------------------------------------------------------------------------- #

def enable(server_id: str, server: dict) -> dict:
    """Validate and start setting crossplay up. Returns the immediate state."""
    sid = str(server_id)
    _raise_if_unsupported(server)
    if progress.is_active(sid):
        raise BedrockError("A crossplay operation is already running.", status_code=409)
    if not _server_stopped(sid, server):
        raise BedrockError("Stop the server before enabling Bedrock crossplay.", status_code=409)

    probe = _try_lock(sid)
    if probe is None:
        raise BedrockError("Another operation is in progress for this server.", status_code=409)
    probe.release()

    port = _ensure_port(sid, server, PORT_KEY)
    if port is None:
        raise BedrockError(
            "No free UDP port in the 19132–19232 range for this server.",
            status_code=400,
        )
    nether_port = _ensure_port(sid, server, NETHER_PORT_KEY, exclude={port})
    if nether_port is None:
        raise BedrockError(
            "No second free UDP port in the 19132–19232 range for this server. "
            "Bedrock crossplay needs one port each for NetherNet and RakNet.",
            status_code=400,
        )

    old_port = _as_int(server.get(PORT_KEY))
    old_nether_port = _as_int(server.get(NETHER_PORT_KEY))
    old_tunnels = [
        str(tunnel_id)
        for tunnel_id in (server.get(TUNNEL_KEY), server.get(RAKNET_TUNNEL_KEY))
        if tunnel_id
    ]
    # A moved port makes the existing tunnel forward to the wrong place.
    stale_tunnels = (
        old_tunnels
        if old_tunnels and (old_port != port or old_nether_port != nether_port)
        else []
    )

    updates = {
        STATE_FLAG: True,
        ERROR_KEY: None,
        PORT_KEY: port,
        NETHER_PORT_KEY: nether_port,
    }
    if stale_tunnels:
        updates.update({
            TUNNEL_KEY: None,
            ADDRESS_KEY: None,
            RAKNET_TUNNEL_KEY: None,
            RAKNET_ADDRESS_KEY: None,
        })
    updated = storage.update_server(sid, updates) or {**server, **updates}

    progress.update(sid, phase="starting", server_id=sid, error=None)
    _spawn("bedrock-enable", sid, _run_enable, sid, updated, stale_tunnels)
    return get_state(updated)


def disable(server_id: str, server: dict) -> dict:
    """Stop using the tunnel and restore Geyser's builtin signaling."""
    sid = str(server_id)
    if is_managed():
        raise BedrockError(_REASON_MESSAGES["managed"], status_code=403)
    if not server.get(STATE_FLAG):
        return get_state(server)
    if progress.is_active(sid):
        raise BedrockError("A crossplay operation is already running.", status_code=409)
    if not _can_teardown(sid, server):
        raise BedrockError("Stop the server before changing Bedrock crossplay.", status_code=409)

    probe = _try_lock(sid)
    if probe is None:
        raise BedrockError("Another operation is in progress for this server.", status_code=409)
    probe.release()

    updated = storage.update_server(sid, {STATE_FLAG: False, ERROR_KEY: None}) or server
    progress.update(sid, phase="starting", server_id=sid, error=None)
    _spawn("bedrock-disable", sid, _run_disable, sid, updated)
    return get_state(updated)


def _raise_if_unsupported(server: dict) -> None:
    reason = support_reason(server)
    if reason is None:
        return
    code = 403 if reason == "managed" else 400
    message = _REASON_MESSAGES.get(reason) or "Bedrock crossplay is not available."
    raise BedrockError(message, status_code=code)


# --------------------------------------------------------------------------- #
# Worker threads
# --------------------------------------------------------------------------- #

def _spawn(label: str, server_id: str, target, *args) -> None:
    threading.Thread(
        target=target,
        args=args,
        name=f"{label}-{server_id}",
        daemon=True,
    ).start()


def _run_enable(
    server_id: str,
    server: dict,
    stale_tunnels: Optional[list[str]] = None,
) -> None:
    sid = str(server_id)
    lock = get_server_lock(sid)
    lock.acquire()
    try:
        for tunnel_id in stale_tunnels or []:
            _delete_stale_tunnel(sid, tunnel_id)

        progress.update(sid, phase="downloading", error=None)

        def _download_progress(detail: dict) -> None:
            progress.update(
                sid,
                phase="downloading",
                plugin=detail.get("label"),
                plugin_version=detail.get("version"),
            )

        plugins_dir = _registry().resolve_content_path(server)
        geyser.ensure_plugins(plugins_dir, on_progress=_download_progress)

        config_path = _config_path(plugins_dir)
        if not config_path.is_file():
            progress.update(sid, phase="generating_config")
            _generate_config(sid, server, config_path)

        progress.update(sid, phase="creating_tunnel")
        secret = playit_tunnel.read_agent_secret()
        if not secret:
            raise BedrockError(
                "Start the playit.gg agent first: its secret is needed to create the tunnels."
            )
        _ensure_agent_running(sid)

        raknet_port = _as_int(server.get(PORT_KEY))
        nether_port = _as_int(server.get(NETHER_PORT_KEY))
        if raknet_port is None or nether_port is None:
            raise BedrockError(
                "The server has no allocated Bedrock ports. Try again."
            )

        tunnels = playit_tunnel.ensure_tunnels(
            secret,
            {
                playit_tunnel.NETHERNET: nether_port,
                playit_tunnel.RAKNET: raknet_port,
            },
            names={
                playit_tunnel.NETHERNET: f"bedrock-nether-{sid}",
                playit_tunnel.RAKNET: f"bedrock-raknet-{sid}",
            },
        )
        nether = tunnels[playit_tunnel.NETHERNET]
        raknet = tunnels[playit_tunnel.RAKNET]

        # Persist the ids even when a tunnel is not ready yet, so a retry reuses
        # it instead of creating a duplicate (playit's free tier has a limit).
        storage.update_server(sid, {
            TUNNEL_KEY: nether.get("tunnel_id"),
            ADDRESS_KEY: nether.get("host"),
            RAKNET_TUNNEL_KEY: raknet.get("tunnel_id"),
            RAKNET_ADDRESS_KEY: raknet.get("address"),
            ERROR_KEY: None,
        })

        if not nether.get("token"):
            raise BedrockError(
                "playit.gg created the NetherNet tunnel but has not issued its "
                "signaling token yet. Try again in a moment."
            )

        progress.update(sid, phase="patching_config")
        # Geyser only re-registers with playit's signaling service when its
        # cached provider state is gone — otherwise it keeps announcing the
        # address of a previous tunnel. Clear it while the server is stopped.
        _clear_provider_state(plugins_dir)
        config_patch.patch_geyser_config(
            config_path,
            token=str(nether["token"]),
            raknet_port=raknet_port,
            nether_port=nether_port,
        )

        missing = [
            TRANSPORT_LABELS[tunnel_type]
            for tunnel_type, result in tunnels.items()
            if not result.get("ready")
        ]
        if missing:
            raise BedrockError(
                "playit.gg has not published the "
                + " and ".join(missing)
                + " address yet. The tunnels are ready — try again in a moment."
            )

        state = get_state(storage.get_server(sid) or server)
        progress.update(
            sid,
            phase="done",
            error=None,
            address=state.get("address"),
            addresses=state.get("addresses"),
        )
        logger.info(
            "bedrock: crossplay ready for %s (raknet %s, nethernet %s)",
            sid,
            raknet_port,
            nether_port,
        )
    except Exception as exc:  # noqa: BLE001 - worker must always record a terminal state
        message = _describe_failure(exc, sid)
        storage.update_server(sid, {ERROR_KEY: message})
        progress.update(sid, phase="failed", error=message)
    finally:
        lock.release()


def _run_disable(server_id: str, server: dict) -> None:
    sid = str(server_id)
    lock = get_server_lock(sid)
    lock.acquire()
    try:
        plugins_dir = _registry().resolve_content_path(server)
        config_path = _config_path(plugins_dir)

        progress.update(sid, phase="patching_config", error=None)
        config_patch.unpatch_geyser_config(config_path)

        tunnel_ids = [
            str(tunnel_id)
            for tunnel_id in (server.get(TUNNEL_KEY), server.get(RAKNET_TUNNEL_KEY))
            if tunnel_id
        ]
        if tunnel_ids:
            progress.update(sid, phase="deleting_tunnel")
            secret = playit_tunnel.read_agent_secret()
            if secret:
                for tunnel_id in tunnel_ids:
                    playit_tunnel.delete_tunnel(secret, tunnel_id)

        storage.update_server(sid, {
            TUNNEL_KEY: None,
            ADDRESS_KEY: None,
            RAKNET_TUNNEL_KEY: None,
            RAKNET_ADDRESS_KEY: None,
            ERROR_KEY: None,
        })
        progress.update(sid, phase="done", error=None, address=None, addresses=[])
        logger.info("bedrock: crossplay disabled for %s", sid)
    except Exception as exc:  # noqa: BLE001
        message = _describe_failure(exc, sid)
        storage.update_server(sid, {ERROR_KEY: message})
        progress.update(sid, phase="failed", error=message)
    finally:
        lock.release()


def _delete_stale_tunnel(server_id: str, tunnel_id: str) -> None:
    """Best-effort cleanup when a server's Bedrock port changed."""
    secret = playit_tunnel.read_agent_secret()
    if not secret:
        return
    if playit_tunnel.delete_tunnel(secret, str(tunnel_id)):
        logger.info("bedrock: removed stale tunnel for %s", server_id)


def _describe_failure(exc: Exception, server_id: str) -> str:
    if isinstance(exc, BedrockError) or isinstance(exc, _EXPECTED_ERRORS):
        return str(exc) or type(exc).__name__
    logger.exception("bedrock: unexpected failure for %s", server_id)
    return str(exc) or type(exc).__name__


# --------------------------------------------------------------------------- #
# Config generation (first start) and command/state helpers
# --------------------------------------------------------------------------- #

def _generate_config(server_id: str, server: dict, config_path: Path) -> None:
    """Start the server once so Geyser writes ``config.yml``, then stop it.

    The file is the signal, not a log line: Geyser creates it after loading its
    own defaults, and the file appearing means the plugin is installed and
    happy. The server is always stopped again, even if the wait fails.
    """
    if str(server.get("status") or "").lower() in ("pending", "failed"):
        raise BedrockError("Install the server before enabling Bedrock crossplay.")

    storage.update_server_status(server_id, "starting")
    started = False
    try:
        result = _registry().start_server(server)
        if result.get("status") != "running":
            message = result.get("message") or "the server did not start"
            raise BedrockError(
                f"Could not start the server to generate Geyser's config: {message}"
            )
        started = True

        deadline = time.monotonic() + _CONFIG_TIMEOUT
        while time.monotonic() < deadline:
            if config_path.is_file() and config_path.stat().st_size > 0:
                return
            runtime = _registry().get_status(server_id)
            if runtime.get("status") != "running":
                break
            time.sleep(_CONFIG_POLL_INTERVAL)
    finally:
        if started:
            try:
                _registry().stop_server(server_id)
            except Exception:  # pragma: no cover - defensive
                logger.warning(
                    "bedrock: could not stop %s after config generation",
                    server_id,
                    exc_info=True,
                )
        storage.update_server_status(server_id, "stopped")

    raise BedrockError(
        "Geyser did not generate plugins/Geyser-Spigot/config.yml. "
        "Check the server console for plugin load errors and retry."
    )


def _playit_state() -> tuple[str, bool]:
    try:
        snapshot = playit_agent.get_status()
    except Exception:  # pragma: no cover - defensive
        return "unknown", False
    status = str(snapshot.get("status") or "unknown")
    return status, status == "running"


def _ensure_agent_running(server_id: str, *, timeout: float = _AGENT_START_TIMEOUT) -> None:
    """Start the local playit agent when it is not running yet.

    playit's API happily creates tunnels while the local agent is stopped, but
    then nothing forwards traffic to Geyser: the public address exists and the
    player just times out. Starting the agent here closes that silent failure
    mode, and also re-arms it after a panel restart (a graceful shutdown
    persists playit's "stopped on purpose" flag, so its own auto-start skips).
    """
    if _playit_state()[1]:
        return
    if not playit_tunnel.read_agent_secret():
        return  # the caller reports the actionable "start the agent" error

    playit_agent.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(_AGENT_POLL_INTERVAL)
        status, ready = _playit_state()
        if ready:
            logger.info("bedrock: playit agent is running for %s", server_id)
            return
        if status == "error":
            break
    raise BedrockError(
        "The playit.gg agent did not come up. Open the playit.gg tab, start it, "
        "then retry."
    )


def resume_enabled_agents() -> None:
    """Boot hook: re-arm the playit agent when a server still wants crossplay.

    ``agent.stop()`` (run on every graceful shutdown) persists "disabled", so
    run.py's own auto-start deliberately skips the next boot. Crossplay servers
    still need the agent, hence this one re-arm. Read-only and best effort: a
    storage or platform problem must not stop the panel from starting.
    """
    if is_managed() or not platform_utils.is_linux():
        return
    try:
        servers = storage.get_all_servers()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("bedrock: could not read servers for agent re-arm: %s", exc)
        return
    if not any(server.get(STATE_FLAG) for server in servers):
        return
    if _playit_state()[1] or not playit_tunnel.read_agent_secret():
        return
    try:
        playit_agent.start()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("bedrock: could not re-arm the playit agent: %s", exc)
        return
    logger.info("bedrock: re-armed the playit agent for enabled crossplay servers")


def _server_running(server_id: str) -> bool:
    try:
        return str(_registry().get_status(server_id).get("status")) == "running"
    except Exception:  # pragma: no cover - defensive
        return False


def _server_stopped(server_id: str, server: dict) -> bool:
    persisted = str(server.get("status") or "").lower()
    if persisted in ("pending", "installing", "failed", "starting", "stopping"):
        return False
    return _can_teardown(server_id, server)


def _can_teardown(server_id: str, server: dict) -> bool:
    """Softer stopped check for disabling: a failed install must still be
    clean-up-able, but a live/draining JVM never is."""
    if str(server.get("status") or "").lower() in ("starting", "stopping", "installing"):
        return False
    try:
        manager = _registry().get_manager(server_id)
    except Exception:  # pragma: no cover - defensive
        manager = None
    if manager is not None and (manager.is_running or manager.is_stopping):
        return False
    return not _server_running(server_id)


def _try_lock(server_id: str):
    from backend.server.locks import try_acquire

    return try_acquire(server_id)


# --------------------------------------------------------------------------- #
# Port allocation
# --------------------------------------------------------------------------- #

def _ensure_port(
    server_id: str,
    server: dict,
    key: str,
    *,
    exclude: "set[int] | tuple[int, ...]" = (),
) -> Optional[int]:
    """Pick a free UDP port for one transport, reusing ``server[key]`` if sane.

    ``exclude`` holds the other transport's port for this server, so the two
    listeners never end up on the same socket.
    """
    sid = str(server_id)
    taken = set(int(port) for port in exclude)
    used = {
        int(other[key_name])
        for other in storage.get_all_servers()
        if str(other.get("id")) != sid
        for key_name in (PORT_KEY, NETHER_PORT_KEY)
        if _as_int(other.get(key_name)) is not None
    }
    used |= taken

    existing = _as_int(server.get(key))
    if (
        existing is not None
        and existing in PORT_RANGE
        and existing not in used
        and _port_available(existing, server)
    ):
        return existing

    for candidate in PORT_RANGE:
        if candidate in used:
            continue
        if _port_available(candidate, server):
            return candidate
    return None


def _port_available(port: int, server: dict) -> bool:
    """True when a UDP bind on ``port`` succeeds right now."""
    host = str(server.get("serverIp") or "").strip() or "0.0.0.0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
