"""Tests for the Bedrock crossplay orchestrator.

The worker threads are driven synchronously here: the goal is to pin the
validation rules, the port allocator, and the exact record/progress updates the
UI polls — not thread scheduling.
"""
from __future__ import annotations

import socket
import threading

import pytest

from backend.bedrock import config_patch, progress, service

SECRET = "b" * 64


# ── Fakes ───────────────────────────────────────────────────────────────────

class _FakeStorage:
    def __init__(self, record):
        self.record = dict(record)

    def get_server(self, server_id):
        return dict(self.record) if str(self.record.get("id")) == str(server_id) else None

    def update_server(self, server_id, updates):
        self.record.update(updates)
        return dict(self.record)

    def update_server_status(self, server_id, status):
        self.record["status"] = status
        return dict(self.record)

    def get_all_servers(self):
        return [dict(self.record)]


class _FakeManager:
    def __init__(self, running=False, stopping=False):
        self.is_running = running
        self.is_stopping = stopping


class _FakeRegistry:
    def __init__(self, plugins_dir, *, manager=None, runtime="stopped"):
        self.plugins_dir = plugins_dir
        self.manager = manager
        self.runtime = runtime
        self.started = []
        self.stopped = []

    def resolve_content_path(self, server):
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        return self.plugins_dir

    def get_manager(self, server_id):
        return self.manager

    def get_status(self, server_id):
        return {"status": self.runtime}

    def start_server(self, server):
        self.started.append(server["id"])
        self.runtime = "running"
        return {"status": "running", "message": "started"}

    def stop_server(self, server_id):
        self.stopped.append(server_id)
        self.runtime = "stopped"
        return {"status": "stopped"}


@pytest.fixture(autouse=True)
def _clean_progress():
    progress.clear("srv_1")
    yield
    progress.clear("srv_1")


def _server(**overrides):
    base = {
        "id": "srv_1",
        "name": "Test",
        "loader": "paper",
        "version": "1.21.4",
        "status": "stopped",
        "bedrockPort": 19140,
    }
    base.update(overrides)
    return base


def _wire(monkeypatch, *, storage=None, registry=None, linux=True, managed=False):
    if storage is not None:
        monkeypatch.setattr(service, "storage", storage)
    if registry is not None:
        monkeypatch.setattr(service, "_registry", lambda: registry)
    monkeypatch.setattr(service.platform_utils, "is_linux", lambda: linux)
    monkeypatch.setattr(service, "is_managed", lambda: managed)
    monkeypatch.setattr(service, "_playit_state", lambda: ("running", True))
    monkeypatch.setattr(service, "get_server_lock", lambda server_id: threading.RLock())


# ── support_reason ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("overrides,expected", [
    ({"loader": "paper"}, None),
    ({"loader": "purpur"}, None),
    ({"loader": "fabric"}, "unsupported_loader"),
    ({"loader": "vanilla"}, "unsupported_loader"),
    ({"loader": ""}, "unsupported_loader"),
    ({"status": "installing"}, "installing"),
    ({"status": "pending"}, "not_installed"),
    ({"status": "failed"}, "not_installed"),
    ({"status": "running"}, None),
])
def test_support_reason(monkeypatch, overrides, expected):
    _wire(monkeypatch)
    assert service.support_reason(_server(**overrides)) == expected


def test_support_reason_managed_wins(monkeypatch):
    _wire(monkeypatch, managed=True)
    assert service.support_reason(_server()) == "managed"


def test_support_reason_non_linux(monkeypatch):
    _wire(monkeypatch, linux=False)
    assert service.support_reason(_server()) == "unsupported_platform"


# ── display_address ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("abc.playit.gg", "abc.playit.gg:19132"),
    ("abc.playit.gg:1234", "abc.playit.gg:19132"),
    ("203.0.113.9", "203.0.113.9:19132"),
    ("::1", "[::1]:19132"),
    ("[::1]", "[::1]:19132"),
])
def test_display_address(host, expected):
    assert service.display_address(host) == expected


# ── Port allocation ─────────────────────────────────────────────────────────

def test_ensure_port_keeps_a_free_existing_port(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(service, "_port_available", lambda port, server: True)
    monkeypatch.setattr(service, "storage", _FakeStorage(_server(bedrockPort=19140)))

    assert service._ensure_port("srv_1", _server(bedrockPort=19140)) == 19140


def test_ensure_port_avoids_ports_used_by_other_servers(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(service, "_port_available", lambda port, server: True)

    class _Storage(_FakeStorage):
        def get_all_servers(self):
            return [
                {"id": "other", "bedrockPort": 19132},
                {"id": "srv_1", "bedrockPort": 19140},
            ]

    monkeypatch.setattr(service, "storage", _Storage(_server()))
    assert service._ensure_port("srv_1", _server(bedrockPort=19132)) == 19133


def test_ensure_port_skips_unavailable_ports(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(service, "_port_available", lambda port, server: port == 19135)
    monkeypatch.setattr(service, "storage", _FakeStorage(_server(bedrockPort=None)))

    assert service._ensure_port("srv_1", _server(bedrockPort=None)) == 19135


def test_ensure_port_returns_none_when_the_range_is_exhausted(monkeypatch):
    _wire(monkeypatch)
    monkeypatch.setattr(service, "_port_available", lambda port, server: False)
    monkeypatch.setattr(service, "storage", _FakeStorage(_server(bedrockPort=None)))

    assert service._ensure_port("srv_1", _server(bedrockPort=None)) is None


def test_port_available_rejects_a_bound_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert service._port_available(port, {"serverIp": "127.0.0.1"}) is False
    finally:
        sock.close()


# ── enable / disable validation ─────────────────────────────────────────────

def test_enable_marks_the_server_and_spawns_the_worker(monkeypatch):
    storage = _FakeStorage(_server())
    registry = _FakeRegistry.__new__(_FakeRegistry)
    registry.runtime = "stopped"
    registry.manager = None
    _wire(monkeypatch, storage=storage, registry=registry)
    monkeypatch.setattr(service, "_server_stopped", lambda sid, server: True)
    monkeypatch.setattr(service, "_port_available", lambda port, server: True)
    spawned = []
    monkeypatch.setattr(
        service, "_spawn",
        lambda label, sid, target, *args: spawned.append((label, sid, target, args)),
    )

    state = service.enable("srv_1", _server())

    assert storage.record["bedrockCrossplay"] is True
    assert storage.record["bedrockPort"] == 19140
    assert state["enabled"] is True
    assert state["active"] is True
    assert state["phase"] == "starting"
    assert spawned and spawned[0][0] == "bedrock-enable"
    assert spawned[0][2] is service._run_enable


def test_enable_rejects_a_running_server(monkeypatch):
    _wire(monkeypatch, storage=_FakeStorage(_server()))
    monkeypatch.setattr(service, "_server_stopped", lambda sid, server: False)

    with pytest.raises(service.BedrockError) as excinfo:
        service.enable("srv_1", _server())
    assert excinfo.value.status_code == 409


def test_enable_rejects_a_mod_loader(monkeypatch):
    _wire(monkeypatch, storage=_FakeStorage(_server(loader="fabric")))

    with pytest.raises(service.BedrockError) as excinfo:
        service.enable("srv_1", _server(loader="fabric"))
    assert excinfo.value.status_code == 400


def test_enable_rejects_managed(monkeypatch):
    _wire(monkeypatch, storage=_FakeStorage(_server()), managed=True)

    with pytest.raises(service.BedrockError) as excinfo:
        service.enable("srv_1", _server())
    assert excinfo.value.status_code == 403


def test_enable_rejects_a_second_operation(monkeypatch):
    _wire(monkeypatch, storage=_FakeStorage(_server()))
    progress.update("srv_1", phase="downloading")

    with pytest.raises(service.BedrockError) as excinfo:
        service.enable("srv_1", _server())
    assert excinfo.value.status_code == 409


def test_disable_clears_the_flag_and_spawns_the_worker(monkeypatch):
    storage = _FakeStorage(_server(bedrockCrossplay=True))
    _wire(monkeypatch, storage=storage)
    monkeypatch.setattr(service, "_can_teardown", lambda sid, server: True)
    spawned = []
    monkeypatch.setattr(
        service, "_spawn",
        lambda label, sid, target, *args: spawned.append((label, target, args)),
    )

    state = service.disable("srv_1", _server(bedrockCrossplay=True))

    assert storage.record["bedrockCrossplay"] is False
    assert state["enabled"] is False
    assert spawned[0][0] == "bedrock-disable"


def test_disable_is_a_noop_when_already_off(monkeypatch):
    _wire(monkeypatch, storage=_FakeStorage(_server(bedrockCrossplay=False)))
    assert service.disable("srv_1", _server(bedrockCrossplay=False))["enabled"] is False


def test_disable_still_works_for_a_failed_server(monkeypatch):
    _wire(monkeypatch, storage=_FakeStorage(_server(status="failed", bedrockCrossplay=True)))
    monkeypatch.setattr(service, "_can_teardown", lambda sid, server: True)
    monkeypatch.setattr(service, "_spawn", lambda *a, **k: None)

    assert service.disable("srv_1", _server(status="failed", bedrockCrossplay=True))["enabled"] is False


def test_can_teardown_refuses_a_live_jvm(monkeypatch):
    registry = _FakeRegistry.__new__(_FakeRegistry)
    registry.manager = _FakeManager(running=True)
    registry.runtime = "stopped"
    _wire(monkeypatch, registry=registry)

    assert service._can_teardown("srv_1", _server(status="failed")) is False


# ── Worker: enable ──────────────────────────────────────────────────────────

def _prepare_worker(monkeypatch, tmp_path, *, config=True):
    plugins = tmp_path / "plugins"
    config_path = plugins / "Geyser-Spigot" / "config.yml"
    if config:
        config_path.parent.mkdir(parents=True)
        config_path.write_text("bedrock:\n  port: 19132\n", encoding="utf-8")

    storage = _FakeStorage(_server())
    registry = _FakeRegistry(plugins)
    _wire(monkeypatch, storage=storage, registry=registry)
    monkeypatch.setattr(service.geyser, "ensure_plugins", lambda plugins_dir, on_progress=None: {})

    return storage, registry, plugins, config_path


def test_run_enable_downloads_tunnels_and_patches(monkeypatch, tmp_path):
    storage, _registry, plugins, config_path = _prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(service.playit_tunnel, "read_agent_secret", lambda: SECRET)
    calls = {}

    def _ensure_tunnel(secret, port, name=None):
        calls.update(secret=secret, port=port, name=name)
        return {"tunnel_id": "t1", "token": "tok", "host": "abc.playit.gg", "created": True}

    monkeypatch.setattr(service.playit_tunnel, "ensure_tunnel", _ensure_tunnel)

    service._run_enable("srv_1", dict(storage.record))

    assert calls == {"secret": SECRET, "port": 19140, "name": "bedrock-srv_1"}
    assert storage.record["bedrockTunnelId"] == "t1"
    assert storage.record["bedrockAddress"] == "abc.playit.gg"
    assert storage.record["bedrockError"] is None
    assert config_patch.read_value(config_path, "bedrock.signaling.nxs.token") == "tok"
    assert config_patch.read_value(config_path, "bedrock.transport") == "nethernet"
    entry = progress.get("srv_1")
    assert entry["phase"] == "done"
    assert entry["address"] == "abc.playit.gg:19132"


def test_run_enable_records_a_missing_playit_secret(monkeypatch, tmp_path):
    storage, _registry, _plugins, config_path = _prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(service.playit_tunnel, "read_agent_secret", lambda: None)

    service._run_enable("srv_1", dict(storage.record))

    entry = progress.get("srv_1")
    assert entry["phase"] == "failed"
    assert "playit.gg agent" in entry["error"]
    assert storage.record["bedrockError"] == entry["error"]
    # The config was never touched without a token.
    assert config_patch.read_value(config_path, "bedrock.signaling.nxs.token") is None


def test_run_enable_surfaces_a_missing_token(monkeypatch, tmp_path):
    storage, _registry, _plugins, _config_path = _prepare_worker(monkeypatch, tmp_path)
    monkeypatch.setattr(service.playit_tunnel, "read_agent_secret", lambda: SECRET)
    monkeypatch.setattr(
        service.playit_tunnel, "ensure_tunnel",
        lambda secret, port, name=None: {
            "tunnel_id": "t1", "token": None, "host": "abc.playit.gg", "created": False,
        },
    )

    service._run_enable("srv_1", dict(storage.record))

    assert progress.get("srv_1")["phase"] == "failed"
    assert "token" in progress.get("srv_1")["error"]
    # The id is kept so a retry reuses the tunnel instead of duplicating it.
    assert storage.record["bedrockTunnelId"] == "t1"


def test_run_enable_generates_the_config_when_absent(monkeypatch, tmp_path):
    storage, registry, plugins, config_path = _prepare_worker(monkeypatch, tmp_path, config=False)
    monkeypatch.setattr(service.playit_tunnel, "read_agent_secret", lambda: SECRET)
    monkeypatch.setattr(
        service.playit_tunnel, "ensure_tunnel",
        lambda secret, port, name=None: {
            "tunnel_id": "t1", "token": "tok", "host": "abc.playit.gg", "created": True,
        },
    )

    def _start(server):
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("bedrock:\n  port: 19132\n", encoding="utf-8")
        return {"status": "running"}

    registry.start_server = _start

    service._run_enable("srv_1", dict(storage.record))

    assert registry.stopped == ["srv_1"]
    assert storage.record["status"] == "stopped"
    assert progress.get("srv_1")["phase"] == "done"


def test_generate_config_raises_when_geyser_writes_nothing(monkeypatch, tmp_path):
    plugins = tmp_path / "plugins"
    registry = _FakeRegistry(plugins)
    _wire(monkeypatch, storage=_FakeStorage(_server()), registry=registry)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)

    def _start(server):
        registry.runtime = "stopped"
        return {"status": "running"}

    registry.start_server = _start

    with pytest.raises(service.BedrockError, match="config.yml"):
        service._generate_config("srv_1", _server(), plugins / "Geyser-Spigot" / "config.yml")
    assert registry.stopped == ["srv_1"]


# ── Worker: disable ─────────────────────────────────────────────────────────

def test_run_disable_unpatches_and_deletes_the_tunnel(monkeypatch, tmp_path):
    plugins = tmp_path / "plugins"
    config_path = plugins / "Geyser-Spigot" / "config.yml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "bedrock:\n  transport: nethernet\n  signaling:\n    mode: nxs\n"
        "    nxs:\n      token: 'tok'\n",
        encoding="utf-8",
    )
    storage = _FakeStorage(_server(bedrockCrossplay=False, bedrockTunnelId="t1"))
    _wire(monkeypatch, storage=storage, registry=_FakeRegistry(plugins))
    monkeypatch.setattr(service.playit_tunnel, "read_agent_secret", lambda: SECRET)
    deleted = []
    monkeypatch.setattr(
        service.playit_tunnel, "delete_tunnel",
        lambda secret, tunnel_id: deleted.append(tunnel_id) or True,
    )

    service._run_disable("srv_1", dict(storage.record))

    assert deleted == ["t1"]
    assert storage.record["bedrockTunnelId"] is None
    assert storage.record["bedrockAddress"] is None
    assert config_patch.read_value(config_path, "bedrock.signaling.mode") == "builtin"
    assert config_patch.read_value(config_path, "bedrock.transport") == "raknet"
    assert progress.get("srv_1")["phase"] == "done"


def test_run_disable_without_a_config_still_clears_the_record(monkeypatch, tmp_path):
    plugins = tmp_path / "plugins"
    storage = _FakeStorage(_server(bedrockCrossplay=False, bedrockTunnelId=None))
    _wire(monkeypatch, storage=storage, registry=_FakeRegistry(plugins))

    service._run_disable("srv_1", dict(storage.record))

    assert progress.get("srv_1")["phase"] == "done"


# ── HTTP surface ────────────────────────────────────────────────────────────

def _create_server(**overrides):
    from backend.server import storage

    return storage.create_server({"name": "t", "version": "1.20", **overrides})


def test_status_endpoint_returns_the_snapshot(client, tmp_servers_root):
    server = _create_server(loader="paper", status="stopped")

    resp = client.get(f"/api/servers/{server['id']}/bedrock")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["server_id"] == server["id"]
    assert body["enabled"] is False
    assert body["client_port"] == 19132
    assert "supported" in body and "reason" in body


def test_progress_endpoint_reports_inactive_initially(client, tmp_servers_root):
    server = _create_server(loader="paper", status="stopped")

    resp = client.get(f"/api/servers/{server['id']}/bedrock/progress")

    assert resp.status_code == 200
    assert resp.get_json()["active"] is False


def test_enable_endpoint_refuses_unsupported_loaders(client, tmp_servers_root):
    server = _create_server(loader="fabric", status="stopped")

    resp = client.post(f"/api/servers/{server['id']}/bedrock/enable")

    assert resp.status_code == 400
    assert resp.get_json()["error"]


def test_bedrock_endpoints_404_for_unknown_servers(client, tmp_servers_root):
    assert client.get("/api/servers/srv_nope/bedrock").status_code == 404
    assert client.post("/api/servers/srv_nope/bedrock/enable").status_code == 404
