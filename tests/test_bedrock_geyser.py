"""Tests for the GeyserMC download client.

Every network call is stubbed: the point is the API contract (latest/build
metadata, spigot-only artefacts, hash verification, the "already current" skip),
not whether download.geysermc.org is reachable from CI.
"""
from __future__ import annotations

import pytest
import requests

from backend.bedrock import geyser


class _Response:
    def __init__(self, payload, *, status=200, json_error=False):
        self._payload = payload
        self.status_code = status
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("boom", response=self)

    def json(self):
        if self._json_error:
            raise ValueError("not JSON")
        return self._payload


class _Session:
    """Minimal requests.Session stand-in that records requested URLs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if not self._responses:
            raise AssertionError(f"unexpected request for {url}")
        value = self._responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _latest(project, name, sha, version="2.12.1", build=1299):
    return {
        "project": project,
        "version": version,
        "build": build,
        "downloads": {
            "spigot": {"name": name, "sha256": sha, "size": 1234},
            "velocity": {"name": "ignored.jar", "sha256": "deadbeef"},
        },
    }


def test_fetch_latest_picks_the_spigot_download():
    session = _Session([_Response(_latest("geyser", "Geyser-Spigot.jar", "AB" * 32))])
    meta = geyser.fetch_latest("geyser", session=session)

    assert session.urls == [
        "https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest"
    ]
    assert meta["name"] == "Geyser-Spigot.jar"
    assert meta["sha256"] == "ab" * 32
    assert meta["version"] == "2.12.1"
    assert meta["build"] == 1299
    assert meta["label"] == "Geyser"


def test_fetch_latest_rejects_http_errors():
    session = _Session([_Response({}, status=503)])
    with pytest.raises(geyser.GeyserError, match="download API"):
        geyser.fetch_latest("geyser", session=session)


def test_fetch_latest_rejects_invalid_json():
    session = _Session([_Response(None, json_error=True)])
    with pytest.raises(geyser.GeyserError, match="invalid JSON"):
        geyser.fetch_latest("floodgate", session=session)


def test_fetch_latest_requires_a_spigot_artefact():
    session = _Session([_Response({"version": "2.12.1", "build": 1, "downloads": {}})])
    with pytest.raises(geyser.GeyserError, match="no Spigot download"):
        geyser.fetch_latest("geyser", session=session)


def test_fetch_latest_requires_a_hash():
    payload = {"version": "2.12.1", "build": 1,
               "downloads": {"spigot": {"name": "Geyser-Spigot.jar"}}}
    session = _Session([_Response(payload)])
    with pytest.raises(geyser.GeyserError, match="no Spigot download"):
        geyser.fetch_latest("geyser", session=session)


def test_download_url_shape():
    assert geyser.download_url("geyser", "2.12.1", 1299) == (
        "https://download.geysermc.org/v2/projects/geyser/"
        "versions/2.12.1/builds/1299/downloads/spigot"
    )


def _stub_downloads(monkeypatch, calls, *, body=b"jar-bytes"):
    def _fake(url, target, **kwargs):
        calls.append((url, target, kwargs))
        target.write_bytes(body)

    monkeypatch.setattr(geyser, "download_with_hash_verify", _fake)


def test_ensure_plugins_downloads_both_projects(tmp_path, monkeypatch):
    calls = []
    _stub_downloads(monkeypatch, calls)
    session = _Session([
        _Response(_latest("geyser", "Geyser-Spigot.jar", "11" * 32)),
        _Response(_latest("floodgate", "floodgate-spigot.jar", "22" * 32, version="2.2.5", build=141)),
    ])

    progress = []
    result = geyser.ensure_plugins(tmp_path / "plugins", on_progress=progress.append, session=session)

    assert set(result) == {"geyser", "floodgate"}
    assert result["geyser"]["downloaded"] is True
    assert (tmp_path / "plugins" / "Geyser-Spigot.jar").is_file()
    assert (tmp_path / "plugins" / "floodgate-spigot.jar").is_file()
    assert [p["label"] for p in progress] == ["Geyser", "Floodgate"]
    # Hash + retries are handed to the shared downloader.
    assert calls[0][2]["sha256"] == "11" * 32
    assert calls[1][2]["sha256"] == "22" * 32


def test_ensure_plugins_skips_a_current_file(tmp_path, monkeypatch):
    calls = []
    _stub_downloads(monkeypatch, calls)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "Geyser-Spigot.jar"
    target.write_bytes(b"jar-bytes")

    session = _Session([
        _Response(_latest("geyser", "Geyser-Spigot.jar", geyser.sha256_file(target))),
        _Response(_latest("floodgate", "floodgate-spigot.jar", "33" * 32, version="2.2.5", build=141)),
    ])

    result = geyser.ensure_plugins(plugins, session=session)

    assert result["geyser"]["downloaded"] is False
    assert result["floodgate"]["downloaded"] is True
    assert len(calls) == 1
    assert calls[0][0].startswith("https://download.geysermc.org/v2/projects/floodgate/")


def test_ensure_plugins_redownloads_on_hash_mismatch(tmp_path, monkeypatch):
    calls = []
    _stub_downloads(monkeypatch, calls)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "Geyser-Spigot.jar").write_bytes(b"stale")

    session = _Session([
        _Response(_latest("geyser", "Geyser-Spigot.jar", "44" * 32)),
        _Response(_latest("floodgate", "floodgate-spigot.jar", "55" * 32, version="2.2.5", build=141)),
    ])

    geyser.ensure_plugins(plugins, session=session)
    assert (plugins / "Geyser-Spigot.jar").read_bytes() == b"jar-bytes"
    assert len(calls) == 2


def test_ensure_plugins_wraps_download_failures(tmp_path, monkeypatch):
    def _explode(url, target, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(geyser, "download_with_hash_verify", _explode)
    session = _Session([_Response(_latest("geyser", "Geyser-Spigot.jar", "66" * 32))])

    with pytest.raises(geyser.GeyserError, match="Could not download Geyser"):
        geyser.ensure_plugins(tmp_path / "plugins", session=session)
