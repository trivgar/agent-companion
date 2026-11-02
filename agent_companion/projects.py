"""The one list of project folders the dashboard may act in.

``[projects]`` in config.toml names each folder, as ``name = "path"`` or a
list of ``{ name, path }`` rows. The Files tab and the New session launcher
both read it. A list row may also carry the launcher's ``key``, ``label`` and
tmux ``prefix`` (``rows()``). ``[new_session].projects``, where the launcher
first kept its own list, is still read so an existing config keeps working; a
name in both takes the ``[projects]`` path.
"""
from __future__ import annotations

from . import config

EXTRAS = ("key", "label", "prefix")


def _rows(raw: object) -> list[tuple[str, str, dict]]:
    """``(name, path, row)`` from a table or a list of rows; malformed rows are
    skipped. A table's ``name = "path"`` carries nothing more, so its row is
    empty."""
    if isinstance(raw, dict):
        items = [(name, path, {}) for name, path in raw.items()]
    elif isinstance(raw, (list, tuple)):
        items = [(row.get("name"), row.get("path"), row) for row in raw if isinstance(row, dict)]
    else:
        items = []
    out = []
    for name, path, row in items:
        if isinstance(name, str) and isinstance(path, str) and name.strip() and path.strip():
            out.append((" ".join(name.split()), path, row))
    return out


def pairs(raw: object) -> list[tuple[str, str]]:
    """``(name, path)`` from a table or a list of rows; malformed rows are skipped."""
    return [(name, path) for name, path, _ in _rows(raw)]


def _configured() -> list[tuple[str, str, dict]]:
    legacy = config.get("new_session", {})
    found = _rows(config.get("projects", {}))
    found += _rows(legacy.get("projects") if isinstance(legacy, dict) else None)
    seen, out = set(), []
    for name, path, row in found:
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append((name, path, row))
    return out


def configured() -> list[tuple[str, str]]:
    """Every configured project, ``[projects]`` first, each name once."""
    return [(name, path) for name, path, _ in _configured()]


def rows() -> list[dict]:
    """``configured()`` as ``{name, path, key, label, prefix}``: the last three
    are what the project's row says for New session, or ``None`` where it
    gives no text."""
    return [{"name": name, "path": path,
             **{k: row[k] if isinstance(row.get(k), str) else None for k in EXTRAS}}
            for name, path, row in _configured()]
