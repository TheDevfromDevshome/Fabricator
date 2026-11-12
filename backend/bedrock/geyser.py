"""GeyserMC download API client and plugin provisioning.

Geyser and Floodgate are published as build artefacts on
``download.geysermc.org``. We always take the *latest* build of each project,
verify the upstream-published SHA-256, and drop the Spigot variant into the
server's ``plugins/`` folder. Nothing here knows about playit — this module is
pure "make sure the two JARs are present and current".
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

import requests

from backend.server.installer.base import download_with_hash_verify

logger = logging.getLogger(__name__)

_API_BASE = "https://download.geysermc.org/v2/projects"
_DOWNLOAD_SUFFIX = "downloads/spigot"
_TIMEOUT = 30

# project slug -> human label, used only for progress/error messages.
PROJECTS = {
    "geyser": "Geyser",
    "floodgate": "Floodgate",
}


class GeyserError(RuntimeError):
    """A GeyserMC API problem or an unusable artefact."""


def fetch_latest(
    project: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: int = _TIMEOUT,
) -> dict:
    """Return the latest Spigot artefact metadata for ``project``.

    The response carries the build's version/build number and a ``downloads``
    map. We only care about the ``spigot`` entry, which is what a
    Paper/Purpur/Folia server loads.
    """
    http = session or requests
    url = f"{_API_BASE}/{project}/versions/latest/builds/latest"
    try:
        response = http.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise GeyserError(
            f"Could not reach the {PROJECTS.get(project, project)} download API "
            f"({type(exc).__name__})."
        ) from None
    except ValueError:
        raise GeyserError(
            f"The {PROJECTS.get(project, project)} download API returned invalid JSON."
        ) from None

    if not isinstance(payload, dict):
        raise GeyserError(
            f"The {PROJECTS.get(project, project)} download API returned an unexpected body."
        )

    spigot = (payload.get("downloads") or {}).get("spigot")
    if not isinstance(spigot, dict) or not spigot.get("name") or not spigot.get("sha256"):
        raise GeyserError(
            f"{PROJECTS.get(project, project)} has no Spigot download in its latest build."
        )

    return {
        "project": project,
        "label": PROJECTS.get(project, project),
        "version": str(payload.get("version") or "latest"),
        "build": payload.get("build"),
        "name": str(spigot["name"]),
        "sha256": str(spigot["sha256"]).lower(),
        "size": spigot.get("size"),
    }


def download_url(project: str, version: str, build: object) -> str:
    """Direct URL for the Spigot artefact of a specific build."""
    return f"{_API_BASE}/{project}/versions/{version}/builds/{build}/{_DOWNLOAD_SUFFIX}"


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_plugins(
    plugins_dir: str | Path,
    *,
    on_progress: Optional[Callable[[dict], None]] = None,
    session: Optional[requests.Session] = None,
) -> dict:
    """Download Geyser + Floodgate into ``plugins_dir`` when needed.

    An existing file whose SHA-256 matches upstream is left alone, so repeated
    enables (or a retry after a later step failed) do not re-download tens of
    megabytes. Returns one metadata record per project.
    """
    target_dir = Path(plugins_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}
    for project in PROJECTS:
        metadata = fetch_latest(project, session=session)
        target = target_dir / metadata["name"]

        already_current = False
        if target.is_file():
            try:
                already_current = sha256_file(target) == metadata["sha256"]
            except OSError:
                already_current = False

        if already_current:
            logger.info(
                "bedrock: %s %s build %s already present",
                metadata["label"], metadata["version"], metadata["build"],
            )
            results[project] = {
                **metadata,
                "path": str(target),
                "downloaded": False,
            }
            continue

        if on_progress is not None:
            on_progress({
                "project": project,
                "label": metadata["label"],
                "version": metadata["version"],
                "build": metadata["build"],
            })

        url = download_url(project, metadata["version"], metadata["build"])
        try:
            download_with_hash_verify(
                url,
                target,
                sha256=metadata["sha256"],
                session=session,
                retries=2,
            )
        except Exception as exc:
            # Keep the message actionable without leaking the URL's query (none
            # here) or a stack trace into the UI.
            raise GeyserError(
                f"Could not download {metadata['label']} "
                f"({metadata['version']} build {metadata['build']}): {exc}"
            ) from exc

        logger.info(
            "bedrock: downloaded %s %s build %s",
            metadata["label"], metadata["version"], metadata["build"],
        )
        results[project] = {
            **metadata,
            "path": str(target),
            "downloaded": True,
        }

    return results
