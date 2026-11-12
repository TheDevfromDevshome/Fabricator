"""Tests for the playit.gg NetherNet tunnel client.

The wire format is the risky part: playit's private API expects a nested
``ports``/``origin.config.fields`` payload and delivers ``nethernet_token``
asynchronously through the list endpoint. These tests pin that shape and the
reuse/poll logic without touching the network.
"""
from __future__ import annotations

import pytest

from backend.bedrock import playit_tunnel

SECRET = "a" * 64


# ── Helpers ──────────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


def _tunnel(
    tunnel_id="t1",
    port=19140,
    token=None,
    kind="nethernet",
    local_ip="127.0.0.1",
    addresses=None,
):
    return {
        "id": tunnel_id,
        "tunnel_type": {"type": kind},
        "connect_addresses": addresses
        if addresses is not None
        else [{"type": "auto", "address": "abc.playit.gg"}],
        "nethernet_token": token,
        "origin": {
            "details": {
                "config_data": {
                    "fields": [
                        {"name": "local_ip", "value": local_ip},
                        {"name": "local_port", "value": str(port)},
                    ]
                }
            }
        },
    }


# ── HTTP layer ───────────────────────────────────────────────────────────────

def test_post_sends_the_agent_key_and_returns_the_body(monkeypatch):
    seen = {}

    def _fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return _Response({"status": "success", "data": {"id": "t1"}})

    monkeypatch.setattr(playit_tunnel.requests, "post", _fake_post)

    body = playit_tunnel._post("/v1/tunnels/create", {"a": 1}, SECRET)

    assert body["data"] == {"id": "t1"}
    assert seen["url"] == "https://api.playit.gg/v1/tunnels/create"
    assert seen["headers"]["Authorization"] == "Agent-Key " + SECRET
    assert seen["json"] == {"a": 1}


def test_post_rejects_a_bad_agent_key_without_leaking_it(monkeypatch):
    monkeypatch.setattr(
        playit_tunnel.requests,
        "post",
        lambda *a, **k: _Response({"status": "error"}, status=403),
    )

    with pytest.raises(playit_tunnel.PlayitTunnelError) as excinfo:
        playit_tunnel._post("/v1/tunnels/list", {}, SECRET)

    assert excinfo.value.code == "unauthorized"
    assert SECRET not in str(excinfo.value)


def test_post_maps_known_error_codes(monkeypatch):
    body = {"status": "error", "error": {"type": "AgentVersionTooOld"}}
    monkeypatch.setattr(playit_tunnel.requests, "post", lambda *a, **k: _Response(body))

    with pytest.raises(playit_tunnel.PlayitTunnelError) as excinfo:
        playit_tunnel._post("/v1/tunnels/create", {}, SECRET)

    assert "v1.2.0" in str(excinfo.value)
    assert excinfo.value.code == "AgentVersionTooOld"


def test_post_rejects_unreadable_bodies(monkeypatch):
    class _Broken(_Response):
        def json(self):
            raise ValueError("html")

    monkeypatch.setattr(playit_tunnel.requests, "post", lambda *a, **k: _Broken({}, 502))

    with pytest.raises(playit_tunnel.PlayitTunnelError) as excinfo:
        playit_tunnel._post("/v1/tunnels/list", {}, SECRET)
    assert excinfo.value.code == "bad_response"


def test_post_turns_network_errors_into_a_friendly_message(monkeypatch):
    def _explode(*a, **k):
        raise playit_tunnel.requests.ConnectionError("dns down")

    monkeypatch.setattr(playit_tunnel.requests, "post", _explode)

    with pytest.raises(playit_tunnel.PlayitTunnelError, match="Could not reach playit.gg"):
        playit_tunnel._post("/v1/tunnels/list", {}, SECRET)


# ── Create / list / delete ───────────────────────────────────────────────────

def test_create_tunnel_payload(monkeypatch):
    captured = {}

    def _fake_post(path, payload, secret, timeout=15):
        captured.update(path=path, payload=payload)
        return {"status": "success", "data": {"id": "t-42"}}

    monkeypatch.setattr(playit_tunnel, "_post", _fake_post)

    tunnel_id = playit_tunnel.create_tunnel(SECRET, 19140, name="bedrock-srv_1")

    assert tunnel_id == "t-42"
    assert captured["path"] == "/v1/tunnels/create"
    payload = captured["payload"]
    assert payload["ports"] == {"type": "tunnel-type", "details": "nethernet"}
    assert payload["enabled"] is True
    assert payload["name"] == "bedrock-srv_1"
    fields = payload["origin"]["data"]["config"]["fields"]
    assert {"name": "local_ip", "value": "127.0.0.1"} in fields
    # local_port travels as a string.
    assert {"name": "local_port", "value": "19140"} in fields
    assert {"name": "proxy_protocol", "value": "false"} in fields


def test_create_tunnel_requires_an_id(monkeypatch):
    monkeypatch.setattr(
        playit_tunnel, "_post", lambda *a, **k: {"status": "success", "data": {}}
    )
    with pytest.raises(playit_tunnel.PlayitTunnelError, match="no tunnel id"):
        playit_tunnel.create_tunnel(SECRET, 19140)


@pytest.mark.parametrize("body", [
    {"status": "success", "data": {"tunnels": [{"id": "a"}, "junk"]}},
    {"status": "success", "data": [{"id": "b"}]},
    {"status": "success", "tunnels": [{"id": "c"}]},
])
def test_list_tunnels_accepts_every_response_shape(monkeypatch, body):
    monkeypatch.setattr(playit_tunnel, "_post", lambda *a, **k: body)
    tunnels = playit_tunnel.list_tunnels(SECRET)
    assert all(isinstance(t, dict) for t in tunnels)
    assert len(tunnels) == 1


def test_delete_tunnel_is_idempotent_on_failure(monkeypatch):
    calls = []

    def _fake_post(path, payload, secret, timeout=15):
        calls.append((path, payload))
        raise playit_tunnel.PlayitTunnelError("TunnelNotFound", code="TunnelNotFound")

    monkeypatch.setattr(playit_tunnel, "_post", _fake_post)
    assert playit_tunnel.delete_tunnel(SECRET, "t1") is False
    assert calls == [("/tunnels/delete", {"tunnel_id": "t1"})]


def test_delete_tunnel_success(monkeypatch):
    monkeypatch.setattr(playit_tunnel, "_post", lambda *a, **k: {"status": "success"})
    assert playit_tunnel.delete_tunnel(SECRET, "t1") is True


# ── Matching ────────────────────────────────────────────────────────────────

def test_find_tunnel_matches_type_and_port():
    tunnels = [
        _tunnel("wrong-port", port=19141),
        _tunnel("wrong-type", port=19140, kind="minecraft-java"),
        _tunnel("match", port=19140),
    ]
    found = playit_tunnel.find_tunnel(tunnels, 19140)
    assert found["id"] == "match"


def test_find_tunnel_prefers_the_requested_local_ip():
    tunnels = [
        _tunnel("other-ip", port=19140, local_ip="10.0.0.5"),
        _tunnel("right-ip", port=19140, local_ip="127.0.0.1"),
    ]
    found = playit_tunnel.find_tunnel(tunnels, 19140, local_ip="127.0.0.1")
    assert found["id"] == "right-ip"


def test_find_tunnel_reads_rundata_agent_config():
    tunnel = {
        "id": "t1",
        "tunnel_type": "nethernet",
        "agent_config": {
            "fields": [
                {"name": "local_port", "value": "19140"},
                {"name": "local_ip", "value": "127.0.0.1"},
            ]
        },
    }
    assert playit_tunnel.find_tunnel([tunnel], 19140)["id"] == "t1"


# ── ensure_tunnel ───────────────────────────────────────────────────────────

def test_ensure_tunnel_reuses_an_existing_tunnel(monkeypatch):
    existing = _tunnel("t1", token="tok")
    monkeypatch.setattr(playit_tunnel, "list_tunnels", lambda secret: [existing])

    def _must_not_create(*a, **k):
        raise AssertionError("create_tunnel should not be called")

    monkeypatch.setattr(playit_tunnel, "create_tunnel", _must_not_create)

    result = playit_tunnel.ensure_tunnel(SECRET, 19140)

    assert result == {
        "tunnel_id": "t1",
        "token": "tok",
        "host": "abc.playit.gg",
        "created": False,
    }


def test_ensure_tunnel_polls_until_the_token_arrives(monkeypatch):
    created = _tunnel("t-new", token=None)
    with_token = _tunnel("t-new", token="tok-9")
    responses = [[], [created], [with_token]]

    def _list(secret):
        return responses.pop(0) if responses else [with_token]

    monkeypatch.setattr(playit_tunnel, "list_tunnels", _list)
    monkeypatch.setattr(playit_tunnel, "create_tunnel", lambda *a, **k: "t-new")
    monkeypatch.setattr(playit_tunnel.time, "sleep", lambda seconds: None)

    result = playit_tunnel.ensure_tunnel(SECRET, 19140, token_timeout=5.0, poll_interval=0.01)

    assert result["created"] is True
    assert result["tunnel_id"] == "t-new"
    assert result["token"] == "tok-9"


def test_ensure_tunnel_keeps_the_id_when_no_token_is_issued(monkeypatch):
    monkeypatch.setattr(playit_tunnel, "list_tunnels", lambda secret: [])
    monkeypatch.setattr(playit_tunnel, "create_tunnel", lambda *a, **k: "t-new")

    result = playit_tunnel.ensure_tunnel(SECRET, 19140, token_timeout=0.0)

    assert result["tunnel_id"] == "t-new"
    assert result["token"] is None
    assert result["created"] is True


# ── connect_host ────────────────────────────────────────────────────────────

def test_connect_host_prefers_domain_over_raw_address():
    tunnel = _tunnel(
        addresses=[
            {"type": "addr4", "address": "203.0.113.5"},
            {"type": "domain", "address": "funny-name.playit.gg:19132"},
        ]
    )
    assert playit_tunnel.connect_host(tunnel) == "funny-name.playit.gg"


def test_connect_host_falls_back_to_display_address():
    tunnel = {"display_address": "fallback.playit.gg:19132"}
    assert playit_tunnel.connect_host(tunnel) == "fallback.playit.gg"


def test_connect_host_handles_ipv6():
    tunnel = _tunnel(addresses=[{"type": "addr6", "address": "[2001:db8::1]:19132"}])
    assert playit_tunnel.connect_host(tunnel) == "2001:db8::1"


def test_connect_host_returns_none_without_addresses():
    assert playit_tunnel.connect_host({}) is None


# ── Secret ──────────────────────────────────────────────────────────────────

def test_read_agent_secret_accepts_hex(tmp_path, monkeypatch):
    path = tmp_path / "secret"
    path.write_text(SECRET + "\n", encoding="utf-8")
    monkeypatch.setattr(playit_tunnel.playit_binary, "secret_path", lambda: path)

    assert playit_tunnel.read_agent_secret() == SECRET


@pytest.mark.parametrize("content", ["", "short", "z" * 64])
def test_read_agent_secret_rejects_garbage(tmp_path, monkeypatch, content):
    path = tmp_path / "secret"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(playit_tunnel.playit_binary, "secret_path", lambda: path)

    assert playit_tunnel.read_agent_secret() is None


def test_read_agent_secret_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        playit_tunnel.playit_binary, "secret_path", lambda: tmp_path / "absent"
    )
    assert playit_tunnel.read_agent_secret() is None
