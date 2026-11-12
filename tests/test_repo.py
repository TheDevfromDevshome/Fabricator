"""Tests for the central update-source configuration (backend/repo.py)."""
from __future__ import annotations

import pytest

import backend.repo as repo_module
from backend.repo import DEFAULT_GITHUB_REPO, get_github_repo, raw_installer_url


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv("FABRICATOR_REPO", raising=False)


def test_default_repo_is_the_fork():
    assert DEFAULT_GITHUB_REPO == "TheDevfromDevshome/Fabricator"
    assert get_github_repo() == DEFAULT_GITHUB_REPO


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("FABRICATOR_REPO", "someone/elsewhere")
    assert get_github_repo() == "someone/elsewhere"


def test_blank_override_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FABRICATOR_REPO", "   ")
    assert get_github_repo() == DEFAULT_GITHUB_REPO


def test_raw_installer_url_uses_default_repo():
    assert raw_installer_url() == (
        "https://raw.githubusercontent.com/"
        f"{DEFAULT_GITHUB_REPO}/main/tools/install.sh"
    )


def test_raw_installer_url_accepts_explicit_repo_and_branch():
    assert raw_installer_url("owner/name", branch="dev") == (
        "https://raw.githubusercontent.com/owner/name/dev/tools/install.sh"
    )


def test_installer_defaults_match_python_default():
    """The shell installer must not drift from the Python updater source."""
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "tools" / "install.sh"
    ).read_text(encoding="utf-8")
    owner, _, name = DEFAULT_GITHUB_REPO.partition("/")
    assert f'GITHUB_OWNER="{owner}"' in script
    assert f'GITHUB_REPO="{name}"' in script
