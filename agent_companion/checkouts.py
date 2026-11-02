"""The checkouts Specs and Files read, grouped by repository.

``checkout_dirs`` names folders whose child folders are checkouts, such as a
workspace folder and the folder its git worktrees live in; a listed folder is
never itself a checkout. With none listed, the shared ``[projects]`` rows are
the checkouts. A repository's checkouts form one group, the one to show first
at its head: the main checkout, else a name in ``checkout_prefer``.
``checkout_skip`` names are never shown, nor read through.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import config
from . import projects as _projects


def dirs() -> tuple[Path, ...]:
    return tuple(p.expanduser() for p in config.paths("checkout_dirs"))


def _names(key: str) -> frozenset[str]:
    value = config.get(key) or ()
    if isinstance(value, str):
        value = value.split(os.pathsep)
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(v for v in value if isinstance(v, str) and v)


def skip() -> frozenset[str]:
    return _names("checkout_skip")


def prefer() -> frozenset[str]:
    return _names("checkout_prefer")


def key() -> tuple:
    """What the discovery depends on, for caches: a changed setting is a new scan."""
    return dirs(), skip(), prefer(), tuple(_projects.configured())


def repo_of(d: Path) -> str:
    """Which git repository a checkout belongs to: a worktree's .git is a file
    naming <repo>/.git/worktrees/<name>, the main checkout's is the folder."""
    g = d / ".git"
    try:
        if g.is_dir():
            return str(g)
        m = re.match(r"gitdir:\s*(.+?)/worktrees/[^/]+\s*$", g.read_text(errors="replace").strip())
        return m.group(1) if m else str(d)
    except OSError:
        return str(d)


def candidates() -> list[tuple[str, Path]]:
    """(name, folder) for every folder that may be a checkout, a name taken once."""
    roots = dirs()
    out: list[tuple[str, Path]] = []
    if not roots:
        for name, value in _projects.configured():
            try:
                d = Path(value).expanduser().resolve(strict=True)
            except OSError:
                continue
            if d.is_dir() and "/" not in name:
                out.append((name, d))
    else:
        listed = {str(r) for r in roots}
        for parent in roots:
            try:
                kids = sorted(parent.iterdir())
            except OSError:
                continue
            for d in kids:
                if d.name.startswith(".") or str(d) in listed:
                    continue
                out.append((d.name, d))
    unique, seen = [], set()
    for n, d in out:
        if n not in seen:
            seen.add(n)
            unique.append((n, d))
    return unique


def grouped(keep) -> list[list[tuple[str, Path]]]:
    """The candidates `keep(folder)` accepts, grouped by repository, the head
    first, then by name; skipped names dropped."""
    gone, first = skip(), prefer()
    groups: dict[str, list[tuple[int, str, Path]]] = {}
    for name, d in candidates():
        if not keep(d):
            continue
        rank = 0 if (d / ".git").is_dir() else 1 if name in first else 2
        groups.setdefault(repo_of(d), []).append((rank, name, d))
    out = []
    for members in groups.values():
        members.sort(key=lambda m: (m[0], m[1]))
        kept = [(n, d) for _, n, d in members if n not in gone]
        if kept:
            out.append(kept)
    return out
