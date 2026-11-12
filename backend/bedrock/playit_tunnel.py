"""playit.gg NetherNet tunnel management.

playit's web UI creates NetherNet tunnels through the same private API the
agent uses. We talk to it directly with the agent secret Fabricator already
persists, avoiding a second claim flow:

    POST /v1/tunnels/create   -> {"status":"success","data":{"id":"<uuid>"}}
    POST /v1/tunnels/list     -> tunnels incl. ``nethernet_token``
    POST /tunnels/delete      -> remove a tunnel (note: no ``/v1`` prefix)

``nethernet_token`` is issued asynchronously after creation, so callers poll
the list endpoint until it appears. The token and the agent secret are never
logged or included in exception messages.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterable, Optional

import requests

from backend.playit import binary as playit_binary

logger = logging.getLogger(__name__)

_API_BASE = "https://api.playit.gg"
_CREATE_PATH = "/v1/tunnels/create"
_LIST_PATH = "/v1/tunnels/list"
_DELETE_PATH = "/tunnels/delete"

_TIMEOUT = 15
_TOKEN_TIMEOUT = 30.0
_TOKEN_POLL_INTERVAL = 1.0

NETHERNET = "nethernet"
DEFAULT_LOCAL_IP = "127.0.0.1"

# Same shape check the agent uses before putting the secret in an HTTP header.
_RE_SECRET = re.compile(r"^[a-fA-F0-9]+$")

_ADDRESS_PRIORITY = ("auto", "domain", "addr4", "ip4", "addr6", "ip6")

_ERROR_MESSAGES = {
    "AgentNotFound": "playit.gg could not find this agent. Restart the playit.gg agent and try again.",
    "AgentVersionTooOld": "The playit agent is too old for NetherNet tunnels. Update it to v1.2.0 or newer.",
    "RequiresPlayitPremium": "This playit.gg account needs Premium for a NetherNet tunnel.",
    "InvalidTunnelConfig": "playit.gg rejected the tunnel configuration.",
    "AgentDisabledOverLimit": "The playit agent has reached its tunnel limit.",
    "TunnelNotFound": "playit.gg could not find that tunnel.",
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
        code = _error_code(body)
        raise PlayitTunnelError(_error_message(body, status), code=code)

    return body


def _error_code(body: dict) -> Optional[str]:
    error = body.get("error")
    if isinstance(error, dict):
        raw = error.get("type") or error.get("code") or error.get("message")
    elif isinstance(error, str):
        raw = error
    else:
        raw = body.get("code")
    return str(raw) if raw else None


def _error_message(body: dict, status: int) -> str:
    code = _error_code(body)
    if code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    detail = body.get("message") if isinstance(body.get("message"), str) else None
    if detail:
        return detail
    if code:
        # Keep the API's symbolic code readable rather than dumping the body.
        return f"playit.gg rejected the request ({code})."
    return f"playit.gg rejected the request (HTTP {status})."


# --------------------------------------------------------------------------- #
# Tunnel operations
# --------------------------------------------------------------------------- #

def create_tunnel(
    secret: str,
    local_port: int,
    *,
    local_ip: str = DEFAULT_LOCAL_IP,
    name: Optional[str] = None,
) -> str:
    """Create a NetherNet tunnel and return its id."""
    payload: dict = {
        "ports": {"type": "tunnel-type", "details": NETHERNET},
        "origin": {
            "type": "agent",
            "data": {
                "config": {
                    "fields": [
                        {"name": "local_ip", "value": local_ip},
                        {"name": "local_port", "value": str(int(local_port))},
                        {"name": "proxy_protocol", "value": "false"},
                    ]
                }
            },
        },
        "enabled": True,
    }
    if name:
        payload["name"] = name

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
    local_ip: Optional[str] = None,
) -> Optional[dict]:
    """Locate the NetherNet tunnel forwarding to ``local_port``."""
    wanted_port = int(local_port)
    fallback: Optional[dict] = None
    for tunnel in tunnels:
        if not isinstance(tunnel, dict):
            continue
        if _tunnel_type(tunnel) != NETHERNET:
            continue
        if _tunnel_local_port(tunnel) != wanted_port:
            continue
        if local_ip:
            existing_ip = _tunnel_field(tunnel, "local_ip")
            if existing_ip and existing_ip != local_ip:
                continue
        if local_ip is None:
            return tunnel
        if _tunnel_field(tunnel, "local_ip") == local_ip:
            return tunnel
        fallback = fallback or tunnel
    return fallback


def ensure_tunnel(
    secret: str,
    local_port: int,
    *,
    local_ip: str = DEFAULT_LOCAL_IP,
    name: Optional[str] = None,
    token_timeout: float = _TOKEN_TIMEOUT,
    poll_interval: float = _TOKEN_POLL_INTERVAL,
) -> dict:
    """Reuse or create the NetherNet tunnel for ``local_port``.

    Returns ``{"tunnel_id", "token", "host", "created"}``. ``token`` may still
    be ``None`` when playit has not issued it within ``token_timeout`` — the
    caller decides whether that is fatal (it is for Geyser, but the tunnel id
    is preserved so a retry reuses the same tunnel instead of creating a
    duplicate).
    """
    tunnels = list_tunnels(secret)
    tunnel = find_tunnel(tunnels, local_port, local_ip=local_ip)
    created = False

    if tunnel is None:
        tunnel_id = create_tunnel(secret, local_port, local_ip=local_ip, name=name)
        created = True
        tunnel = {"id": tunnel_id}
    else:
        tunnel_id = str(tunnel.get("id") or "")

    token = tunnel.get("nethernet_token")
    deadline = time.monotonic() + max(0.0, token_timeout)
    while not token and time.monotonic() < deadline:
        time.sleep(max(0.05, poll_interval))
        tunnels = list_tunnels(secret)
        tunnel = _by_id(tunnels, tunnel_id) or find_tunnel(
            tunnels, local_port, local_ip=local_ip
        )
        if tunnel:
            token = tunnel.get("nethernet_token")

    return {
        "tunnel_id": tunnel_id,
        "token": str(token) if token else None,
        "host": connect_host(tunnel or {}),
        "created": created,
    }


def connect_host(tunnel: dict) -> Optional[str]:
    """Best public hostname for a NetherNet tunnel, without the port.

    NetherNet clients always use port 19132, so the host is all the UI needs;
    playit may report a bare hostname, a ``host:port`` pair, or an IPv6 literal.
    """
    candidates = list(_connect_addresses(tunnel))
    for preferred in _ADDRESS_PRIORITY:
        for kind, address in candidates:
            if kind == preferred and address:
                return _strip_port(address)
    for _kind, address in candidates:
        if address:
            return _strip_port(address)

    display = tunnel.get("display_address")
    if isinstance(display, str) and display.strip():
        return _strip_port(display)
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


def _connect_addresses(tunnel: dict):
    raw = tunnel.get("connect_addresses")
    if isinstance(raw, dict):
        # Defensive: some responses wrap the list under a type key.
        raw = raw.get("addresses") or list(raw.values())
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return

    for entry in raw:
        address = None
        kind = None
        if isinstance(entry, str):
            address = entry
        elif isinstance(entry, dict):
            kind = entry.get("type")
            address = (
                entry.get("address")
                or entry.get("addr")
                or entry.get("host")
            )
            port = entry.get("port")
            if address and port and ":" not in str(address):
                address = f"{address}:{port}"
        if address:
            yield (str(kind) if kind else None), str(address)


def _strip_port(value: str) -> str:
    value = value.strip()
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            return value[1:end]
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value
