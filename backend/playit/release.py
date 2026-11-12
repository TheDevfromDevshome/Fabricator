"""Pinned playit-agent release: version, per-asset sha256, arch mapping.

The single source of truth for WHICH playit binaries Fabricator trusts. Read by

  * :mod:`backend.playit.provision` — to download and verify them, and
  * :mod:`backend.playit.binary`    — to classify whatever is already on disk.

Bumping ``PLAYIT_VERSION`` requires refreshing EVERY digest in ``SHA256`` from
https://api.github.com/repos/playit-cloud/playit-agent/releases/tags/<ver>
(the ``digest`` field on each asset). Provisioning fails closed on a mismatch,
so a half-updated pin breaks loudly rather than installing something unchecked.

playit publishes these assets for Linux only — every other platform resolves to
no arch and the caller degrades gracefully (Windows never gets this far; see
``agent.get_status``).
"""
from __future__ import annotations

import platform
from typing import Optional

# Bump together with every digest below.
#
# v1.2.0 is the minimum playit-agent release that supports NetherNet tunnels
# (`tunnel_type: nethernet`), which the Bedrock-crossplay feature provisions.
# Older pins still work for Java tunnels but the API rejects nethernet creates
# with `AgentVersionTooOld`.
PLAYIT_VERSION = "v1.2.0"

_DOWNLOAD_BASE = (
    "https://github.com/playit-cloud/playit-agent/releases/download/{version}/{asset}"
)

# Executable names as Fabricator installs (and discovers) them, independent of
# the arch-suffixed asset names on the release page.
DAEMON_NAME = "playit"
CLI_NAME = "playit-cli"
BINARY_NAMES = (DAEMON_NAME, CLI_NAME)

# asset name -> sha256. Verified against the GitHub release API; identical to
# the PLAYIT_SHA256 table in tools/install.sh (which stays the installer's copy
# until that script is migrated onto `python -m backend.playit.provision`).
SHA256: dict[str, str] = {
    "playit-linux-amd64":       "602dea3288d5bad4c3addba70ecf6535b9ae085b8d4f2fc2fe9cde913bda78d0",
    "playit-linux-aarch64":     "3a6d69c348803c7041c49312345c18b1e9cb7dd7fc2adfbf2217e50d0d7c9a34",
    "playit-linux-armv7":       "d6314b5e83318bf0b8731aae95e81f499ed4338d30e5e8a8bf866cdebcf6e8e0",
    "playit-linux-i686":        "14e8d468565a0fdba14af436280af960b6fe189b5bfade132d68134a196a6dbb",
    "playit-cli-linux-amd64":   "a10cd77866bdd81837848ad08b6629012637f2662e7c0b7d9310bcdb619feebd",
    "playit-cli-linux-aarch64": "d4cc2cd2e6f4bc9614a2b764eba60a5e0656d71ff8a2720a061e2333daef8940",
    "playit-cli-linux-armv7":   "f5e8bb223d707e51b94a2a35dea038d02d6235295ef19f7bdabfedd39424c58d",
    "playit-cli-linux-i686":    "34bf2c4ff47f70d5ae225c8e94866b53a88151f7c0c8ba0e0a57dc89d39696b0",
}

# platform.machine() -> playit asset arch suffix. Mirrors the `uname -m` case
# in tools/install.sh, plus the aliases Python reports on some hosts (e.g.
# "arm64" under Docker/QEMU on Apple silicon, "amd64" on a few distros).
_MACHINE_TO_ARCH = {
    "x86_64":  "amd64",
    "amd64":   "amd64",
    "aarch64": "aarch64",
    "arm64":   "aarch64",
    "armv7l":  "armv7",
    "armv7":   "armv7",
    "armv8l":  "armv7",
    "i386":    "i686",
    "i486":    "i686",
    "i586":    "i686",
    "i686":    "i686",
}


def resolve_arch(machine: Optional[str] = None) -> Optional[str]:
    """Map a machine string to a playit asset arch, or None if unsupported.

    Defaults to the running host's ``platform.machine()``. Not cached: the
    override argument exists so tests can exercise every branch, and the
    uncached path costs one syscall on a status poll at worst.
    """
    raw = (machine if machine is not None else platform.machine()).strip().lower()
    return _MACHINE_TO_ARCH.get(raw)


def asset_name(binary: str, arch: str) -> str:
    """Release-asset name for an installed binary name + arch.

    ``("playit", "amd64")`` -> ``"playit-linux-amd64"``.
    """
    return f"{binary}-linux-{arch}"


def asset_url(asset: str) -> str:
    """Full GitHub download URL for a release asset of the pinned version."""
    return _DOWNLOAD_BASE.format(version=PLAYIT_VERSION, asset=asset)


def expected_sha256(binary: str, arch: Optional[str] = None) -> Optional[str]:
    """Pinned digest for ``binary`` on ``arch`` (default: this host's arch).

    None when the arch is unsupported or the table has no entry for it — both
    mean "we cannot vouch for this binary", never "any binary will do".
    """
    if arch is None:
        arch = resolve_arch()
    if arch is None:
        return None
    return SHA256.get(asset_name(binary, arch))
