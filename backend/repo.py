"""The GitHub repository this build takes its panel releases from.

This is a community fork of ``philderks/Fabricator``. It publishes its own
releases, so every place that needs "where do updates come from" reads this
module instead of hardcoding an owner/name pair:

  * ``backend/system/update_service.py`` — release lookup, Windows ``.exe``
    download and the ``curl | bash`` updater URL,
  * ``tools/cli.py`` — ``fabricator update`` release lookup,
  * ``tools/install.sh`` — tarball download (mirrors ``FABRICATOR_REPO``).

Operators can point a deployment somewhere else without editing code:

    FABRICATOR_REPO=owner/name

Deliberately a top-level module (like ``backend/managed.py``): importing
``backend.core.repo`` would run ``backend/core/__init__.py``, which imports the
app factory, which imports ``backend.system.routes`` → back into the update
service. Keep this module dependency-free.
"""
from __future__ import annotations

import os

# Update source for builds cut from this repository. Keep in sync with the
# GITHUB_OWNER/GITHUB_REPO defaults in tools/install.sh.
DEFAULT_GITHUB_REPO = "TheDevfromDevshome/Fabricator"

DEFAULT_BRANCH = "main"


def get_github_repo() -> str:
    """Return ``owner/name``, honouring the ``FABRICATOR_REPO`` override."""
    override = (os.environ.get("FABRICATOR_REPO") or "").strip()
    return override or DEFAULT_GITHUB_REPO


def raw_installer_url(repo: str | None = None, branch: str = DEFAULT_BRANCH) -> str:
    """Raw URL of ``tools/install.sh`` in ``repo`` (the ``curl | bash`` updater)."""
    return (
        f"https://raw.githubusercontent.com/{repo or get_github_repo()}"
        f"/{branch}/tools/install.sh"
    )
