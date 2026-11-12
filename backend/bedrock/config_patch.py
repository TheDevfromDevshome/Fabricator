"""In-place editor for Geyser's ``config.yml``.

Geyser ships a plain, 2-space-indented YAML document full of comments and
examples. Pulling in PyYAML just to flip a handful of scalars would round-trip
the whole file and throw that away, so this module edits lines in place:
locate a dotted key path by its indentation stack, replace only the scalar on
that line, and append a missing block at the right place.

The patcher is intentionally narrow. It understands the subset Geyser emits —
mapping keys, scalar values, comments, blank lines — and leaves everything else
(including inline comments) untouched. That is enough for the ten values the
crossplay setup owns; it is not a general YAML library.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".bedrock.bak"
_INDENT_UNIT = 2

# Values written when crossplay is enabled. ``quote`` forces single quotes
# around a token so YAML never mistakes a leading digit/symbol for a number.
_ENABLE_VALUES: tuple[tuple[str, object, bool], ...] = (
    ("bedrock.address", "0.0.0.0", False),
    ("bedrock.transport", "nethernet", False),
    ("bedrock.clone-remote-port", False, False),
    ("bedrock.signaling.mode", "nxs", False),
    ("bedrock.signaling.stun-discovery", False, False),
    ("bedrock.signaling.nxs.endpoint", "https://nethernet.playit.gg", False),
    ("java.auth-type", "floodgate", False),
)

# Values restored when crossplay is disabled. The port and nxs endpoint are
# left alone: both are harmless with builtin signaling, and the next enable
# rewrites them anyway.
_DISABLE_VALUES: tuple[tuple[str, object, bool], ...] = (
    ("bedrock.transport", "raknet", False),
    ("bedrock.clone-remote-port", False, False),
    ("bedrock.signaling.mode", "builtin", False),
    ("bedrock.signaling.nxs.token", "", True),
)

_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(.*)$")
_SAFE_SCALAR_RE = re.compile(r"^[A-Za-z0-9_.:/+-]+$")


class ConfigPatchError(RuntimeError):
    """The config could not be read, written, or verified."""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def patch_geyser_config(
    path: str | Path,
    *,
    token: str,
    bedrock_port: int,
    endpoint: str = "https://nethernet.playit.gg",
) -> None:
    """Point Geyser at a playit NetherNet tunnel.

    Writes the ten values playit's guide asks for (address/port/transport,
    signaling mode + private token, Floodgate auth). The original file is
    copied to ``config.yml.bedrock.bak`` before the first change, then the
    result is re-read and verified so a partially-written edit can never be
    reported as success.
    """
    values = (
        ("bedrock.port", int(bedrock_port), False),
        ("bedrock.webrtc-port", 0, False),
        ("bedrock.signaling.nxs.token", token, True),
        ("bedrock.signaling.nxs.endpoint", endpoint, False),
        *_ENABLE_VALUES,
    )
    _apply(path, values, verify=True)


def unpatch_geyser_config(path: str | Path) -> None:
    """Return Geyser to its builtin signaling service.

    Called when the user turns crossplay off. The Geyser/Floodgate plugins stay
    installed — re-enabling is then a patch-only operation again.
    """
    if not Path(path).exists():
        return
    _apply(path, _DISABLE_VALUES, verify=True)


def read_value(path_or_text: str | Path, dotted: str) -> str | None:
    """Return the scalar at ``dotted`` without quotes, or ``None`` if absent.

    ``path_or_text`` is a path when it names an existing file, otherwise the
    literal YAML text (handy for tests). Comments and blank lines are ignored.
    """
    if isinstance(path_or_text, Path):
        text = path_or_text.read_text(encoding="utf-8")
    else:
        candidate = Path(path_or_text)
        if "\n" not in path_or_text and candidate.exists():
            text = candidate.read_text(encoding="utf-8")
        else:
            text = path_or_text

    found = _find_key(text.splitlines(), tuple(dotted.split(".")))
    if found is None:
        return None
    _, _, raw = found
    value, _ = _split_value_comment(raw)
    return _unquote(value)


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _apply(
    path: str | Path,
    values: tuple[tuple[str, object, bool], ...],
    *,
    verify: bool,
) -> None:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigPatchError(f"Geyser config not found at {config_path}")

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigPatchError(f"Could not read {config_path.name}: {exc}") from exc

    _backup(config_path)

    lines = text.splitlines()
    for dotted, value, quote in values:
        _ensure_path(lines, tuple(dotted.split(".")), value, quote)

    new_text = "\n".join(lines)
    if text.endswith("\n") or not text:
        new_text += "\n"
    _write_atomic(config_path, new_text)

    if verify:
        for dotted, value, _quote in values:
            actual = read_value(new_text, dotted)
            expected = _plain_scalar(value)
            if actual != expected:
                raise ConfigPatchError(
                    f"Verification failed for {dotted}: expected {expected!r}, "
                    f"found {actual!r}"
                )


def _backup(path: Path) -> Path:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise ConfigPatchError(f"Could not back up {path.name}: {exc}") from exc
    return backup


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ConfigPatchError(f"Could not write {path.name}: {exc}") from exc


def _iter_keys(lines: list[str]):
    """Yield ``(index, indent, key, parent_path)`` for every mapping key."""
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        match = _KEY_RE.match(stripped)
        if not match:
            continue
        key = match.group(1)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = tuple(name for _, name in stack)
        yield index, indent, key, parent
        stack.append((indent, key))


def _find_key(lines: list[str], path: tuple[str, ...]):
    for index, indent, key, parent in _iter_keys(lines):
        if parent + (key,) == path:
            _, rest = _split_key_line(lines[index])
            return index, indent, rest
    return None


def _split_key_line(line: str) -> tuple[str, str]:
    """Return ``(key, raw_value)`` from a mapping line."""
    stripped = line.strip()
    key, _, rest = stripped.partition(":")
    return key.strip(), rest.strip()


def _split_value_comment(raw: str) -> tuple[str, str]:
    """Split an inline comment off a raw value, respecting quoted strings."""
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None and (index == 0 or raw[index - 1].isspace()):
            return raw[:index].rstrip(), raw[index:]
    return raw.strip(), ""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == "'":
            return inner.replace("''", "'")
        return inner.replace('\\"', '"')
    return value


def _format_scalar(value: object, *, quote: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = "" if value is None else str(value)
    if quote:
        return "'" + text.replace("'", "''") + "'"
    if text == "":
        return "''"
    if _SAFE_SCALAR_RE.match(text) and not text.startswith(("#", "!", "&", "*")):
        return text
    return "'" + text.replace("'", "''") + "'"


def _plain_scalar(value: object) -> str:
    """The value as ``read_value`` will report it (unquoted, comment-free)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _ensure_path(
    lines: list[str],
    path: tuple[str, ...],
    value: object,
    quote: bool,
) -> None:
    """Create or update the scalar at ``path``.

    Missing parents are created recursively as empty blocks; an existing parent
    that currently has an inline scalar (``nxs: {}``) is promoted to a block
    header so children can be inserted under it.
    """
    found = _find_key(lines, path)
    if found is not None:
        index, _indent, _raw = found
        if value is not None:
            _set_scalar(lines, index, value, quote)
        return

    if len(path) == 1:
        parent_index, parent_indent = -1, -1
    else:
        _ensure_path(lines, path[:-1], None, False)
        parent = _find_key(lines, path[:-1])
        if parent is None:  # pragma: no cover - defensive
            raise ConfigPatchError(f"Could not create parent for {'.'.join(path)}")
        parent_index, parent_indent, raw_parent = parent
        if raw_parent and not raw_parent.startswith("#"):
            key, _ = _split_key_line(lines[parent_index])
            indent = " " * parent_indent
            lines[parent_index] = f"{indent}{key}:"

    child_indent = parent_indent + _INDENT_UNIT if parent_indent >= 0 else 0
    line = f"{' ' * child_indent}{path[-1]}:"
    if value is not None:
        line += f" {_format_scalar(value, quote=quote)}"

    insert_at = _block_end(lines, parent_index, parent_indent)
    lines.insert(insert_at, line)


def _block_end(lines: list[str], parent_index: int, parent_indent: int) -> int:
    """Index after the last line belonging to the parent block."""
    insert_at = parent_index + 1
    for index in range(parent_index + 1, len(lines)):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if stripped.startswith("#"):
            if indent > parent_indent:
                continue
            break
        if indent > parent_indent:
            insert_at = index + 1
            continue
        break
    return insert_at


def _set_scalar(lines: list[str], index: int, value: object, quote: bool) -> None:
    raw = lines[index]
    indent = raw[: len(raw) - len(raw.lstrip(" "))]
    key, rest = _split_key_line(raw)
    _old_value, comment = _split_value_comment(rest)
    line = f"{indent}{key}: {_format_scalar(value, quote=quote)}"
    if comment:
        line += f"  {comment}"
    lines[index] = line
