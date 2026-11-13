"""Tests for the in-place Geyser config editor.

The patcher is a line-oriented editor, not a YAML parser: these tests pin the
behaviour the crossplay setup relies on — exact scalars, preserved comments,
idempotent re-runs, and a backup that is only ever the pre-first-edit file.
"""
from __future__ import annotations

import pytest

from backend.bedrock import config_patch

CONFIG = """\
bedrock:
  address: 0.0.0.0
  port: 19132
  clone-remote-port: false
  webrtc-port: -1
  # Transport: raknet or nethernet
  transport: raknet
  broadcast-port: 19132
  # External signaling (playit.gg)
  signaling:
    mode: builtin
    stun-discovery: true
    nxs:
      token: ''
      endpoint: https://example.invalid
java:
  auth-type: online
  # Leave this alone
  forward-hostname: false
"""

TOKEN = "7f3c1a9e5b2d4c8f0a6e1d3b5c7f9a2e"


def _write(tmp_path, text=CONFIG):
    path = tmp_path / "config.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_patch_writes_every_bedrock_value(tmp_path):
    path = _write(tmp_path)
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)

    assert config_patch.read_value(path, "bedrock.address") == "0.0.0.0"
    assert config_patch.read_value(path, "bedrock.port") == "19140"
    assert config_patch.read_value(path, "bedrock.webrtc-port") == "19141"
    assert config_patch.read_value(path, "bedrock.transport") == "both"
    assert config_patch.read_value(path, "bedrock.clone-remote-port") == "false"
    assert config_patch.read_value(path, "bedrock.signaling.mode") == "nxs"
    assert config_patch.read_value(path, "bedrock.signaling.stun-discovery") == "false"
    assert (
        config_patch.read_value(path, "bedrock.signaling.nxs.endpoint")
        == "https://nethernet.playit.gg"
    )
    assert config_patch.read_value(path, "bedrock.signaling.nxs.token") == TOKEN
    assert config_patch.read_value(path, "java.auth-type") == "floodgate"


def test_patch_keeps_unrelated_keys_and_comments(tmp_path):
    path = _write(tmp_path)
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)
    text = path.read_text(encoding="utf-8")

    assert "broadcast-port: 19132" in text
    assert "# Transport: raknet or nethernet" in text
    assert "# Leave this alone" in text
    assert "forward-hostname: false" in text


def test_patch_preserves_inline_comment(tmp_path):
    path = _write(tmp_path, CONFIG.replace("  transport: raknet\n", "  transport: raknet  # legacy\n"))
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)

    text = path.read_text(encoding="utf-8")
    assert "  transport: both  # legacy" in text


def test_patch_is_idempotent(tmp_path):
    path = _write(tmp_path)
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)
    once = path.read_text(encoding="utf-8")
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)
    assert path.read_text(encoding="utf-8") == once


def test_backup_is_taken_once_before_the_first_edit(tmp_path):
    path = _write(tmp_path)
    original = path.read_text(encoding="utf-8")

    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)
    backup = tmp_path / ("config.yml" + config_patch.BACKUP_SUFFIX)
    assert backup.read_text(encoding="utf-8") == original

    # A second, different patch must not overwrite the pristine backup.
    config_patch.patch_geyser_config(path, token="a" * 32, raknet_port=19141, nether_port=19142)
    assert backup.read_text(encoding="utf-8") == original


def test_patch_quotes_the_token(tmp_path):
    path = _write(tmp_path)
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)
    line = next(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("token:")
    )
    assert line.strip() == f"token: '{TOKEN}'"


def test_patch_creates_missing_blocks(tmp_path):
    path = _write(tmp_path, "bedrock:\n  port: 19132\n")
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)

    assert config_patch.read_value(path, "bedrock.signaling.mode") == "nxs"
    assert config_patch.read_value(path, "bedrock.signaling.nxs.token") == TOKEN
    assert config_patch.read_value(path, "java.auth-type") == "floodgate"


def test_patch_promotes_empty_parent_block(tmp_path):
    path = _write(tmp_path, "bedrock:\n  signaling:\n    nxs: {}\n")
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)

    text = path.read_text(encoding="utf-8")
    assert "nxs:" in text
    assert "nxs: {}" not in text
    assert config_patch.read_value(path, "bedrock.signaling.nxs.token") == TOKEN


def test_unpatch_restores_builtin_signaling(tmp_path):
    path = _write(tmp_path)
    config_patch.patch_geyser_config(path, token=TOKEN, raknet_port=19140, nether_port=19141)

    config_patch.unpatch_geyser_config(path)

    assert config_patch.read_value(path, "bedrock.transport") == "nethernet"
    assert config_patch.read_value(path, "bedrock.webrtc-port") == "0"
    assert config_patch.read_value(path, "bedrock.signaling.mode") == "builtin"
    assert config_patch.read_value(path, "bedrock.signaling.nxs.token") == ""
    # The allocated ports and endpoint are left in place: harmless with builtin
    # signaling and rewritten by the next enable.
    assert config_patch.read_value(path, "bedrock.port") == "19140"


def test_unpatch_missing_file_is_a_noop(tmp_path):
    config_patch.unpatch_geyser_config(tmp_path / "absent.yml")


def test_patch_missing_file_raises(tmp_path):
    with pytest.raises(config_patch.ConfigPatchError):
        config_patch.patch_geyser_config(
            tmp_path / "absent.yml", token=TOKEN, raknet_port=19140, nether_port=19141
        )


def test_read_value_accepts_raw_yaml_text():
    text = "a:\n  b: 'x y'  # note\n"
    assert config_patch.read_value(text, "a.b") == "x y"
    assert config_patch.read_value(text, "a.c") is None


def test_read_value_unquotes_doubled_single_quotes():
    text = "token: 'it''s'\n"
    assert config_patch.read_value(text, "token") == "it's"
