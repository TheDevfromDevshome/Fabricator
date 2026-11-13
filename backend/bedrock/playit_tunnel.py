"""playit.gg tunnel management for Bedrock crossplay.

Fabricator creates two tunnels per server so Bedrock clients can connect either
way:

    nethernet          Minecraft Bedrock (NetherNet) -> Geyser's NXS signaling
    minecraft-bedrock  Minecraft Bedrock (RakNet)    -> Geyser's RakNet listener

Every request uses the agent secret Fabricator already persists, so no second
claim flow is needed. The create body mirrors what playit's own web UI sends
(``protocol`` / ``origin`` / ``endpoint``), which the account API validates
strictly — an older ``ports``/``alloc`` body is rejected with HTTP 400:

    POST /v1/tunnels/create   {"name","protocol","origin","endpoint","enabled"}
    POST /v1/tunnels/list     tunnels incl. ``nethernet_token`` + addresses
    POST /v1/agents/rundata   the agent id the create body needs
    POST /tunnels/delete      {"tunnel_id": ...}

``public_allocations``/``connect_addresses`` and ``nethernet_token`` are issued
asynchronously after creation, so callers poll the list endpoint until they
appear. Secrets and tokens are never logged or included in exception messages.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

import requests

from backend.playit import binary as playit_binary

logger = logging.getLogger(__name__)

_API_BASE = "https://api.playit.gg"
_CREATE_PATH = "/v1/tunnels/create"
_LIST_PATH = "/v1/tunnels/list"
_RUNDATA_PATH = "/v1/agents/rundata"
_DELETE_PATH = "/tunnels/delete"

_TIMEOUT = 20
_READY_TIMEOUT = 120.0
_POLL_INTERVAL = 2.0

# Tunnel types, exactly as playit names them in its own UI.
NETHERNET = "nethernet"
RAKNET = "minecraft-bedrock"
TUNNEL_TYPES = (NETHERNET, RAKNET)
TUNNEL_LABELS = {NETHERNET: "NetherNet", RAKNET: "RakNet"}

DEFAULT_LOCAL_IP = "127.0.0.1"
# NetherNet clients always connect on 19132; playit itself rewrites the port
# when it displays a NetherNet address, so we do the same.
NETHERNET_PORT = 19132
REGION = "global"

# Same shape check the agent uses before putting the secret in an HTTP header.
_RE_SECRET = re.compile(r"^[a-fA-F0-9]+$")

# Domains before raw IPs: the hostname keeps working when playit re-homes the
# allocation, and it reads better in the UI.
_ADDRESS_PRIORITY = ("auto", "domain", "addr4", "ip4", "addr6", "ip6")

_ERROR_MESSAGES = {
    "AgentNotFound": "playit.gg could not find this agent. Restart the playit.gg agent and try again.",
    "AgentVersionTooOld": "The playit agent is too old for Bedrock tunnels. Update it to v1.2.0 or newer.",
    "AgentDisabledOverLimit": "The playit agent has reached its tunnel limit.",
    "RequiresPlayitPremium": "This playit.gg account needs Premium for this tunnel type.",
    "InvalidTunnelConfig": "playit.gg rejected the tunnel configuration.",
    "validation": "playit.gg rejected the tunnel configuration.",
    "TunnelNotFound": "playit.gg could not find that tunnel.",
    "auth": "playit.gg rejected the agent key. Re-claim the playit agent in the playit.gg tab.",
}


class PlayitTunnelError(RuntimeError):
    """A rejected or unreachable playit API call."""

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Secret / HTTP
# --------------------------------------------------------------------------- #

def read_agent_secret() -> Optional[str]:
    """Read the persisted playit agent secret, or ``None`` if absent/invalid."""
    try:
        raw = playit_binary.secret_path().read_text()
    except OSError:
        return None
    secret = raw.strip()
    if len(secret) < 32 or not _RE_SECRET.match(secret):
        return None
    return secret


def _post(
    path: str,
    payload: dict,
    secret: str,
    *,
    timeout: int = _TIMEOUT,
) -> dict:
    """POST to the playit API. Never logs/echoes the secret or raw body."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Agent-Key " + secret,
    }
    try:
        response = requests.post(
            _API_BASE + path,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise PlayitTunnelError(
            f"Could not reach playit.gg ({type(exc).__name__})."
        ) from None

    status = int(getattr(response, "status_code", 0) or 0)
    if status in (401, 403):
        raise PlayitTunnelError(
            "playit.gg rejected the agent key. Re-claim the playit agent in the playit.gg tab.",
            code="unauthorized",
        )

    try:
        body = response.json()
    except ValueError:
        raise PlayitTunnelError(
            f"playit.gg returned an unreadable response (HTTP {status}).",
            code="bad_response",
        ) from None

    if not isinstance(body, dict):
        raise PlayitTunnelError(
            f"playit.gg returned an unexpected response (HTTP {status}).",
            code="bad_response",
        )

    if status >= 400 or body.get("status") == "error":
        raise PlayitTunnelError(_error_message(body, status), code=_error_code(body))

    return body


def _error_code(body: dict) -> Optional[str]:
    code, _detail = _error_fields(body)
    return code


def _error_fields(body: dict) -> tuple[Optional[str], Optional[str]]:
    """``(code, detail)`` from either the current or the legacy error envelope.

    Current: ``{"status":"error","data":{"type":...,"message":...}}``
    Legacy:  ``{"status":"error","error":{"type":...}}``
    """
    for key in ("data", "error"):
        payload = body.get(key)
        if isinstance(payload, dict):
            code = payload.get("type") or payload.get("code")
            detail = payload.get("message")
            return (
                str(code) if code else None,
                detail.strip() if isinstance(detail, str) and detail.strip() else None,
            )
    code = body.get("code")
    detail = body.get("message")
    return (
        str(code) if code else None,
        detail.strip() if isinstance(detail, str) and detail.strip() else None,
    )


def _error_message(body: dict, status: int) -> str:
    code, detail = _error_fields(body)
    if code in _ERROR_MESSAGES:
        base = _ERROR_MESSAGES[code]
        # The API's own wording often names the exact field that failed; keep it
        # alongside the friendly message instead of burying it.
        if detail and detail.lower() not in base.lower():
            return f"{base} ({detail})"
        return base
    if detail:
        return f"playit.gg rejected the request: {detail}"
    if code:
        # Keep the API's symbolic code readable rather than dumping the body.
        return f"playit.gg rejected the request ({code})."
    return f"playit.gg rejected the request (HTTP {status})."


# --------------------------------------------------------------------------- #
# Agent / tunnel operations
# --------------------------------------------------------------------------- #

def get_agent_id(secret: str, tunnels: Optional[Iterable[dict]] = None) -> str:
    """The agent id that ``/v1/tunnels/create`` expects in ``origin.data``.

    ``/v1/agents/rundata`` is the authoritative source; the account tunnel list
    also carries it, which keeps this working on older API builds.
    """
    failure: Optional[PlayitTunnelError] = None
    try:
        body = _post(_RUNDATA_PATH, {}, secret)
    except PlayitTunnelError as exc:
        failure = exc
    else:
        data = body.get("data")
        if isinstance(data, dict) and data.get("agent_id"):
            return str(data["agent_id"])

    if tunnels is None:
        tunnels = list_tunnels(secret)
    for tunnel in tunnels:
        origin = tunnel.get("origin")
        if not isinstance(origin, dict):
            continue
        details = origin.get("details")
        if isinstance(details, dict) and details.get("agent_id"):
            return str(details["agent_id"])

    if failure is not None:
        raise failure
    raise PlayitTunnelError(
        "playit.gg did not report an agent id for this key. Re-claim the playit agent and retry.",
        code="AgentNotFound",
    )


def create_tunnel(
    secret: str,
    local_port: int,
    *,
    tunnel_type: str = NETHERNET,
    local_ip: str = DEFAULT_LOCAL_IP,
    name: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> str:
    """Create a tunnel of ``tunnel_type`` and return its id."""
    if tunnel_type not in TUNNEL_TYPES:
        raise ValueError(f"unsupported playit tunnel type: {tunnel_type}")
    if agent_id is None:
        agent_id = get_agent_id(secret)

    payload: dict = {
        "name": name or f"fabricator-{tunnel_type}",
        "protocol": {"type": "tunnel-type", "details": tunnel_type},
        "origin": {
            "type": "agent",
            "data": {
                "agent_id": agent_id,
                "config": {
                    "fields": [
                        {"name": "local_ip", "value": local_ip},
                        {"name": "local_port", "value": str(int(local_port))},
                    ]
                },
            },
        },
        # "region" is playit's shared network; NetherNet and RakNet both use it.
        "endpoint": {"type": "region", "details": {"region": REGION, "port": None}},
        "enabled": True,
    }

    body = _post(_CREATE_PATH, payload, secret)
    data = body.get("data")
    tunnel_id = None
    if isinstance(data, dict):
        tunnel_id = data.get("id")
    elif isinstance(data, str):
        tunnel_id = data
    tunnel_id = tunnel_id or body.get("id")
    if not tunnel_id:
        raise PlayitTunnelError(
            "playit.gg created no tunnel id in its response.",
            code="bad_response",
        )
    return str(tunnel_id)


def list_tunnels(secret: str) -> list[dict]:
    """Return the account's tunnels as a list of dicts."""
    body = _post(_LIST_PATH, {}, secret)
    data = body.get("data")
    raw: object
    if isinstance(data, dict):
        raw = data.get("tunnels")
    elif isinstance(data, list):
        raw = data
    else:
        raw = body.get("tunnels")
    return [tunnel for tunnel in (raw or []) if isinstance(tunnel, dict)]


def delete_tunnel(secret: str, tunnel_id: str) -> bool:
    """Best-effort removal. Returns ``False`` when playit refuses/offline."""
    try:
        _post(_DELETE_PATH, {"tunnel_id": str(tunnel_id)}, secret)
        return True
    except PlayitTunnelError as exc:
        logger.info("bedrock: could not delete playit tunnel: %s", exc)
        return False


def find_tunnel(
    tunnels: Iterable[dict],
    local_port: int,
    *,
    tunnel_type: str = NETHERNET,
    local_ip: Optional[str] = None,
) -> Optional[dict]:
    """Locate the tunnel of ``tunnel_type`` forwarding to ``local_port``."""
    wanted_port = int(local_port)
    fallback: Optional[dict] = None
    for tunnel in tunnels:
        if not isinstance(tunnel, dict):
            continue
        if _tunnel_type(tunnel) != tunnel_type:
            continue
        if _tunnel_local_port(tunnel) != wanted_port:
            continue
        existing_ip = _tunnel_field(tunnel, "local_ip")
        if local_ip and existing_ip and existing_ip != local_ip:
            continue
        if local_ip is None:
            return tunnel
        if existing_ip == local_ip:
            return tunnel
        fallback = fallback or tunnel
    return fallback


def ensure_tunnels(
    secret: str,
    ports: dict[str, int],
    *,
    local_ip: str = DEFAULT_LOCAL_IP,
    names: Optional[dict[str, str]] = None,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
) -> dict[str, dict]:
    """Reuse or create one tunnel per ``{tunnel_type: local_port}`` entry.

    Creates every missing tunnel first, then polls the list endpoint once per
    interval until each tunnel has an address (and, for NetherNet, its
    signaling token). Returns ``{tunnel_type: result}`` where each result is
    ``{"tunnel_id","token","address","host","created","ready","type","local_port"}``.
    A result whose ``ready`` is ``False`` still carries its tunnel id, so a retry
    reuses the same tunnel instead of creating a duplicate.
    """
    types = [tunnel_type for tunnel_type in ports]
    if not types:
        return {}

    timeout = _READY_TIMEOUT if timeout is None else timeout
    poll = _POLL_INTERVAL if poll_interval is None else poll_interval

    tunnels = list_tunnels(secret)
    agent_id: Optional[str] = None
    entries: dict[str, dict] = {}

    for tunnel_type in types:
        local_port = int(ports[tunnel_type])
        tunnel = find_tunnel(
            tunnels, local_port, tunnel_type=tunnel_type, local_ip=local_ip
        )
        created = tunnel is None
        if created:
            if agent_id is None:
                agent_id = get_agent_id(secret, tunnels)
            tunnel = {
                "id": create_tunnel(
                    secret,
                    local_port,
                    tunnel_type=tunnel_type,
                    local_ip=local_ip,
                    name=(names or {}).get(tunnel_type),
                    agent_id=agent_id,
                )
            }
        entries[tunnel_type] = {"tunnel": tunnel, "created": created}

    pending = {t for t in types if not _ready(entries[t]["tunnel"], t)}
    deadline = time.monotonic() + max(0.0, timeout)
    while pending and time.monotonic() < deadline:
        time.sleep(max(0.05, poll))
        tunnels = list_tunnels(secret)
        for tunnel_type in list(pending):
            entry = entries[tunnel_type]
            tunnel_id = str((entry["tunnel"] or {}).get("id") or "")
            refreshed = _by_id(tunnels, tunnel_id) if tunnel_id else None
            if refreshed is None:
                refreshed = find_tunnel(
                    tunnels,
                    int(ports[tunnel_type]),
                    tunnel_type=tunnel_type,
                    local_ip=local_ip,
                )
            if refreshed is not None:
                entry["tunnel"] = refreshed
            if _ready(entry["tunnel"], tunnel_type):
                pending.discard(tunnel_type)

    results: dict[str, dict] = {}
    for tunnel_type in types:
        tunnel = entries[tunnel_type]["tunnel"] or {}
        results[tunnel_type] = {
            "type": tunnel_type,
            "local_port": int(ports[tunnel_type]),
            "tunnel_id": str(tunnel.get("id") or ""),
            "token": _token(tunnel),
            "address": connect_address(tunnel, tunnel_type),
            "host": connect_host(tunnel, tunnel_type),
            "created": entries[tunnel_type]["created"],
            "ready": _ready(tunnel, tunnel_type),
        }
    return results


def ensure_tunnel(
    secret: str,
    local_port: int,
    *,
    tunnel_type: str = NETHERNET,
    local_ip: str = DEFAULT_LOCAL_IP,
    name: Optional[str] = None,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
) -> dict:
    """Single-tunnel convenience wrapper around :func:`ensure_tunnels`."""
    return ensure_tunnels(
        secret,
        {tunnel_type: local_port},
        local_ip=local_ip,
        names={tunnel_type: name} if name else None,
        timeout=timeout,
        poll_interval=poll_interval,
    )[tunnel_type]


# --------------------------------------------------------------------------- #
# Address extraction
# --------------------------------------------------------------------------- #

def connect_address(tunnel: dict, tunnel_type: str = NETHERNET) -> Optional[str]:
    """Full, client-facing address, or ``None`` while playit is allocating.

    NetherNet always ends in ``:19132`` (the client protocol requires it, and
    playit's UI shows the same); RakNet uses the allocated host *and* port.
    """
    if not isinstance(tunnel, dict):
        return None
    host, port = _best_endpoint(tunnel)
    if not host:
        return None
    if tunnel_type == NETHERNET:
        return _join(host, NETHERNET_PORT)
    if port is None:
        return None
    return _join(host, port)


def connect_host(tunnel: dict, tunnel_type: str = NETHERNET) -> Optional[str]:
    """The address without its port (NetherNet's host portion by default)."""
    address = connect_address(tunnel, tunnel_type)
    if not address:
        return None
    host, _port = _split_host_port(address, None)
    return host


def _ready(tunnel: dict, tunnel_type: str) -> bool:
    if connect_address(tunnel, tunnel_type) is None:
        return False
    return tunnel_type != NETHERNET or bool(_token(tunnel))


def _token(tunnel: dict) -> Optional[str]:
    token = tunnel.get("nethernet_token")
    return str(token) if token else None


def _best_endpoint(tunnel: dict) -> tuple[Optional[str], Optional[int]]:
    entries = list(_address_entries(tunnel))
    for preferred in _ADDRESS_PRIORITY:
        for kind, host, port in entries:
            if kind == preferred and host:
                return host, port
    for _kind, host, port in entries:
        if host:
            return host, port

    # Allocation present but connect_addresses not populated yet.
    for allocation in tunnel.get("public_allocations") or []:
        details = allocation.get("details") if isinstance(allocation, dict) else None
        if not isinstance(details, dict):
            details = allocation if isinstance(allocation, dict) else {}
        host = (
            details.get("auto_domain")
            or details.get("ip_hostname")
            or details.get("ip")
        )
        if host:
            return str(host), _as_int(details.get("port"))

    display = tunnel.get("display_address")
    if isinstance(display, str) and display.strip():
        return _split_host_port(display, None)
    return None, None


def _address_entries(tunnel: dict) -> Iterator[tuple[Optional[str], str, Optional[int]]]:
    """Yield ``(type, host, port)`` for every connect address playit reports."""
    raw = tunnel.get("connect_addresses")
    if isinstance(raw, dict):
        raw = raw.get("addresses") or list(raw.values())
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return

    for entry in raw:
        if isinstance(entry, str):
            host, port = _split_host_port(entry, None)
            if host:
                yield None, host, port
            continue
        if not isinstance(entry, dict):
            continue

        kind = entry.get("type")
        # Current shape nests the address under "value"; older/rundata shapes
        # keep it flat.
        value = entry.get("value")
        container = value if isinstance(value, dict) else entry
        address = (
            container.get("address")
            or container.get("addr")
            or container.get("host")
        )
        if not isinstance(address, str) or not address.strip():
            continue
        fallback = _as_int(
            container.get("port") if container.get("port") is not None
            else container.get("default_port")
        )
        host, port = _split_host_port(address, fallback)
        if host:
            yield (str(kind) if kind else None), host, port


def _split_host_port(value: str, fallback: Optional[int]) -> tuple[str, Optional[int]]:
    """Split ``host[:port]``, handling bracketed and bare IPv6 literals."""
    text = str(value).strip()
    if not text:
        return "", fallback
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return text, fallback
        host = text[1:end]
        rest = text[end + 1:]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, fallback
    if text.count(":") == 1:
        host, _, tail = text.rpartition(":")
        if tail.isdigit() and host:
            return host, int(tail)
        return text, fallback
    return text, fallback


def _join(host: str, port: int) -> str:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _as_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Tunnel shape helpers
# --------------------------------------------------------------------------- #

def _by_id(tunnels: Iterable[dict], tunnel_id: str) -> Optional[dict]:
    for tunnel in tunnels:
        if str(tunnel.get("id") or "") == str(tunnel_id):
            return tunnel
    return None


def _tunnel_type(tunnel: dict) -> Optional[str]:
    raw = tunnel.get("tunnel_type")
    if isinstance(raw, dict):
        raw = raw.get("type") or raw.get("tunnel_type")
    if raw:
        return str(raw)

    ports = tunnel.get("ports")
    if isinstance(ports, dict):
        if str(ports.get("type") or "") == "tunnel-type":
            detail = ports.get("details")
            return str(detail) if detail else None
        if ports.get("type"):
            return str(ports["type"])
    return None


def _tunnel_fields(tunnel: dict) -> list[dict]:
    """Fields from either the account API (origin.details) or rundata."""
    origin = tunnel.get("origin")
    if isinstance(origin, dict):
        details = origin.get("details")
        if isinstance(details, dict):
            for key in ("config_data", "config"):
                container = details.get(key)
                if isinstance(container, dict) and isinstance(container.get("fields"), list):
                    return [f for f in container["fields"] if isinstance(f, dict)]
    agent_config = tunnel.get("agent_config")
    if isinstance(agent_config, dict) and isinstance(agent_config.get("fields"), list):
        return [f for f in agent_config["fields"] if isinstance(f, dict)]
    return []


def _tunnel_field(tunnel: dict, name: str) -> Optional[str]:
    for field in _tunnel_fields(tunnel):
        if field.get("name") == name:
            value = field.get("value")
            return str(value) if value is not None else None
    return None


def _tunnel_local_port(tunnel: dict) -> Optional[int]:
    value = _tunnel_field(tunnel, "local_port")
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
