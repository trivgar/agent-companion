"""The checkouts as files, like a repository view, and editable.

What is visible is what git lists (tracked, plus untracked and not ignored),
less generated folders (SKIP_DIRS), so a .env, a key or node_modules is never
shown, read or written. A path is only ever looked up in that list, and goes
through ``safe_paths`` whenever it is opened, served or saved. A save is
refused if the file changed since it was opened; it commits (on that
checkout's branch, never pushed) only when the file had no uncommitted work in
it before, so an agent's half-done changes are never committed as the person
who saved. ``files_author_name`` and ``files_author_email`` name that person
(a repository's own user.email still wins); without them git's own identity
commits. Every save is a line in ``files_log``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import checkouts as _checkouts
from . import config, restore, safe_paths

MAX_BYTES = 1024 * 1024
LIST_S = 30                       # a checkout's file list is cached this long
GIT_TIMEOUT_S = 20
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", ".env", ".virtualenv"}
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
          ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="files-git")
_lists: dict[str, tuple[float, list[str]]] = {}
_repos: dict = {"at": 0.0, "key": None, "list": None, "paths": {}}


@dataclass(frozen=True)
class Snapshot:
    raw: bytes
    text: str
    mtime_ns: int
    sha256: str


def log_path() -> Path:
    return Path(str(config.get("files_log") or restore.data_dir() / "files.jsonl")).expanduser()


def _author() -> tuple[str, str]:
    name, email = config.get("files_author_name"), config.get("files_author_email")
    return (name if isinstance(name, str) else "", email if isinstance(email, str) else "")


def _git(path: Path, *args: str, timeout: float = GIT_TIMEOUT_S, env: dict | None = None
         ) -> subprocess.CompletedProcess | None:
    """git without optional locks, fsmonitor or an input stream; None when it
    could not run or timed out."""
    try:
        return subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
                              capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              env={**os.environ, **(env or {}), "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None


def _out(r: subprocess.CompletedProcess | None) -> str:
    return r.stdout.decode("utf-8", "replace") if r is not None and r.returncode == 0 else ""


def _branch(d: Path) -> str:
    """The checkout's branch, read off HEAD (git per checkout costs ~0.3 s on a slow drive)."""
    g = d / ".git"
    try:
        if g.is_file():
            m = re.match(r"gitdir:\s*(.+?)\s*$", g.read_text(errors="replace").strip())
            head = Path(m.group(1)) / "HEAD" if m else None
        else:
            head = g / "HEAD"
        ref = head.read_text().strip() if head else ""
    except OSError:
        return ""
    return ref[len("ref: refs/heads/"):] if ref.startswith("ref: refs/heads/") else ref[:8]


def _scan() -> tuple[list[dict], dict[str, Path]]:
    out, paths = [], {}
    for kept in _checkouts.grouped(lambda d: (d / ".git").exists()):
        out.append({"repo": kept[0][0], "checkouts": [{"name": n, "branch": _branch(d)} for n, d in kept]})
        paths.update({n: d for n, d in kept if n not in paths})
    return sorted(out, key=lambda r: r["repo"].lower()), paths


def repos() -> list[dict]:
    """Every repository, its checkouts listed with their branches, the one to
    show first at the head. Cached LIST_S."""
    key = _checkouts.key()
    with _lock:
        if _repos["key"] == key and _repos["list"] is not None and time.time() - _repos["at"] < LIST_S:
            return _repos["list"]
    got, paths = _scan()
    with _lock:
        _repos.update(at=time.time(), key=key, list=got, paths=paths)
    return got


def checkout(name: str) -> Path | None:
    """A checkout's folder, by a name repos() lists, and only then."""
    repos()
    return _repos["paths"].get(name)


def checkout_of(cwd: str) -> str:
    """The checkout a working directory is in (the deepest match), or ''."""
    repos()
    try:
        here = Path(cwd).resolve()
    except (OSError, ValueError):
        return ""
    best = ""
    for name, d in _repos["paths"].items():
        if here.is_relative_to(d.resolve()) and len(str(d)) > len(str(_repos["paths"].get(best, ""))):
            best = name
    return best


def relative(name: str, path: str) -> str | None:
    """A path as git names it within a checkout, or None if it is outside."""
    d = checkout(name)
    try:
        rel = Path(os.path.normpath(path)).relative_to(d) if d else None
    except ValueError:
        return None
    return rel.as_posix() if rel is not None else None


def _skipped(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in PurePosixPath(rel).parts)


def listing(path: Path, fresh: bool = False) -> list[str]:
    """What git lists, never stat-ed one by one: every path is checked when it is used."""
    key = str(path)
    with _lock:
        hit = _lists.get(key)
        if hit and not fresh and time.time() - hit[0] < LIST_S:
            return hit[1]
    r = _git(path, "ls-files", "-co", "--exclude-standard", "-z")
    files = sorted({f for f in _out(r).split("\0") if f and not _skipped(f)})
    with _lock:
        _lists[key] = (time.time(), files)
    return files


def _commit(path: Path, rel: str = "") -> dict | None:
    args = ["log", "-1", "--format=%h%x00%an%x00%at%x00%s"] + (["--", rel] if rel else [])
    parts = _out(_git(path, *args)).strip().split("\0")
    if len(parts) < 4:
        return None
    return {"sha": parts[0], "author": parts[1], "at": int(parts[2] or 0), "subject": parts[3]}


def _readme(files: list[str], prefix: str) -> str:
    for f in files:
        if f.startswith(prefix) and "/" not in f[len(prefix):] and re.fullmatch(r"readme(\.md|\.markdown|\.txt)?", f[len(prefix):], re.I):
            return f
    return ""


def _file(name: str, rel: str) -> tuple[Path, Path] | None:
    """(checkout, file) for a path git lists, inside it with no symlink on the way."""
    path = checkout(name)
    if not path or not rel or rel not in listing(path):
        return None
    f = safe_paths.file(path, rel)
    return (path, f) if f is not None else None


def tree(name: str, folder: str = "") -> dict:
    path = checkout(name)
    if not path:
        return {"ok": False, "detail": "No such project."}
    folder = folder.strip("/")
    prefix = folder + "/" if folder else ""
    head = _pool.submit(_commit, path)                    # git on a slow drive is ~1 s a call: overlap them
    files = listing(path)
    dirs, here = set(), []
    for f in files:
        if not f.startswith(prefix):
            continue
        rest = f[len(prefix):]
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
        else:
            here.append(rest)
    if folder and not dirs and not here:
        return {"ok": False, "detail": "No such folder."}
    readme, text = _readme(files, prefix), ""
    got = _file(name, readme) if readme else None
    if got:
        try:
            with open(got[1], "rb") as fh:
                text = fh.read(MAX_BYTES).decode("utf-8", "replace")
        except OSError:
            pass
    return {"ok": True, "checkout": name, "branch": _branch(path), "folder": folder,
            "dirs": sorted(dirs, key=str.lower), "files": sorted(here, key=str.lower),
            "commit": head.result(), "count": len(files), "readme": readme, "readme_text": text}


def all_paths(name: str) -> dict:
    path = checkout(name)
    if not path:
        return {"ok": False, "detail": "No such project."}
    return {"ok": True, "paths": listing(path)}


def _status(path: Path, rel: str) -> str | None:
    """clean, untracked or modified; None when git could not say."""
    r = _git(path, "status", "--porcelain=v1", "-z", "--", rel)
    if r is None or r.returncode:
        return None
    s = r.stdout.decode("utf-8", "replace")
    if not s:
        return "clean"
    return "untracked" if s.startswith("??") else "modified"


def read(name: str, rel: str) -> dict:
    got = _file(name, rel)
    if not got:
        return {"ok": False, "detail": "No such file."}
    path, f = got
    try:
        size = f.stat().st_size
        with open(f, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return {"ok": False, "detail": f"Could not read it: {exc.strerror or exc}"}
    commit, status = _pool.submit(_commit, path, rel), _pool.submit(_status, path, rel)
    out = {"ok": True, "checkout": name, "path": rel, "bytes": size, "commit": commit.result(),
           "status": status.result() or "unknown", "branch": _branch(path)}
    ext = f.suffix.lower()
    if ext in IMAGES:
        return dict(out, binary=True, image=True, editable=False)
    if b"\0" in raw[:8192]:
        return dict(out, binary=True, editable=False)
    try:
        text = raw[:MAX_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return dict(out, binary=True, editable=False)
    big = len(raw) > MAX_BYTES
    return dict(out, text=text, cut=big, editable=not big, sha=hashlib.sha256(raw).hexdigest(),
                crlf="\r\n" in text)


def raw(name: str, rel: str) -> tuple[Path, str] | None:
    """An image's file and type, for the Raw route."""
    got = _file(name, rel)
    if not got or got[1].suffix.lower() not in IMAGES:
        return None
    return got[1], IMAGES[got[1].suffix.lower()]


def _snapshot(f: Path) -> Snapshot | None:
    """A bounded UTF-8 file read while nothing else wrote it, or None."""
    try:
        before = f.stat()
        if before.st_size > MAX_BYTES:
            return None
        data = f.read_bytes()
        after = f.stat()
    except OSError:
        return None
    if len(data) > MAX_BYTES or b"\0" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        return None
    return Snapshot(data, text, after.st_mtime_ns, hashlib.sha256(data).hexdigest())


def _replace(path: Path, rel: str, f: Path, data: bytes) -> str:
    """Write a sibling, fsync it and rename it over the file; an error in words, or ''."""
    temporary = None
    try:
        # Checked again right before the write: os.replace swaps a symlink
        # itself rather than following it, and safe_paths refuses one anywhere
        # below the checkout.
        if safe_paths.file(path, rel) != f:
            return "The file is no longer inside the project."
        mode = stat.S_IMODE(f.stat().st_mode)
        fd, temporary = tempfile.mkstemp(prefix=f".{f.name}.", dir=f.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, f)
        temporary = None
        return ""
    except OSError as exc:
        return f"Could not write it: {exc.strerror or exc}"
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _log(entry: dict) -> None:
    try:
        target = log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def save(name: str, rel: str, text: str, expect: str, message: str = "", commit: bool = True) -> dict:
    """Write an edit, then commit it unless the file had uncommitted work
    before (an agent's). Refused with the file as it is now when it changed
    since it was opened: `expect` is the sha read() gave."""
    got = _file(name, rel)
    if not got:
        return {"ok": False, "detail": "No such file."}
    path, f = got
    first = _snapshot(f)
    if first is None:
        return {"ok": False, "detail": "This file cannot be edited here."}
    changed = {"ok": False, "conflict": True, "detail": "It changed since you opened it."}
    if first.sha256 != expect:
        return dict(changed, current=read(name, rel))
    before = _status(path, rel)
    # git status can take long enough on a big checkout for another writer to
    # land: the bytes this save read are checked once more.
    again = _snapshot(f)
    if again is None or (again.mtime_ns, again.sha256) != (first.mtime_ns, first.sha256):
        return dict(changed, current=read(name, rel))
    if "\r\n" in first.text:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    data = text.encode("utf-8")
    if data == first.raw:
        return {"ok": True, "unchanged": True, "committed": False}
    error = _replace(path, rel, f, data)
    if error:
        return {"ok": False, "detail": error}
    out = {"ok": True, "committed": False, "sha": hashlib.sha256(data).hexdigest(),
           "status_before": before or "unknown"}
    if commit and before == "clean":
        author, email = _author()
        email = _out(_git(path, "config", "--local", "user.email")).strip() or email
        env = {}
        if author:
            env.update(GIT_AUTHOR_NAME=author, GIT_COMMITTER_NAME=author)
        if email:
            env.update(GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
        msg = (message or "").strip() or f"Update {Path(rel).name}"
        # --only on the one path: whatever else is staged in this checkout stays staged, uncommitted.
        r = _git(path, "commit", "--only", "-m", msg, "--", rel, timeout=60, env=env)
        if r is not None and r.returncode == 0:
            out.update(committed=True, commit=_commit(path, rel))
        else:
            err = (r.stderr or r.stdout) if r is not None else b"git did not run"
            out["commit_error"] = err.decode("utf-8", "replace").strip()[-400:]
    elif commit:
        out["not_committed"] = {"modified": "it had uncommitted changes before your edit",
                                "untracked": "it is a new file git does not track yet"}.get(
                                    before or "", "git could not say whether it had uncommitted changes")
    with _lock:
        _lists.pop(str(path), None)
    _log({"at": time.time(), "checkout": name, "path": rel, "bytes": len(data), "committed": out["committed"],
          "before": before or "unknown"})
    return out
