"""The specs and plans agents write, to read and answer.

Where they are: ``docs/superpowers/specs/`` and ``docs/superpowers/plans/`` in
each checkout (``agent_companion.checkouts``), plus any folder named in
``spec_folders``, listed under its name with kind ``docs``. A document is
named by (project, kind, file name) and resolved inside its own folder: a
name is a plain ``*.md``, never a path, and a symlink is refused, so nothing
else on disk can be read through it.

Read only. The Watcher pushes each new spec once (first sight silent, a burst
summarised), and never starts under pytest.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import checkouts as _checkouts
from . import config, restore, safe_paths

KINDS = ("specs", "plans")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,160}\.md$")
MAX_BYTES = 512 * 1024
SCAN_S = 300                    # a listing is cached this long (the Watcher refreshes it every PUSH_EVERY_S)
PUSH_EVERY_S = 120
BURST = 3                       # more new specs than this at once is one push, not many

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "key": None, "docs": None}
_titles: dict[tuple[str, float, int], str] = {}


def folders() -> dict[str, Path]:
    """``spec_folders``: {name: folder} of extra document folders. In the
    environment, name=path pairs separated like other lists."""
    value = config.get("spec_folders") or {}
    if isinstance(value, str):
        value = dict(part.split("=", 1) for part in value.split(os.pathsep) if "=" in part)
    if not isinstance(value, dict):
        return {}
    return {name: Path(path).expanduser() for name, path in value.items()
            if isinstance(name, str) and name and "/" not in name and isinstance(path, str) and path}


def seen_file() -> Path:
    """Which specs the Watcher has already pushed (``specs_seen_file``)."""
    return Path(str(config.get("specs_seen_file") or restore.data_dir() / "specs-seen.json")).expanduser()


def _docs_in(d: Path) -> dict[str, Path]:
    sp = d / "docs" / "superpowers"
    return {k: sp / k for k in KINDS if (sp / k).is_dir()}


def checkouts() -> list[list[tuple[str, dict[str, Path]]]]:
    """Every checkout with specs or plans, grouped by repository, the one to
    read first at the head of each group."""
    return [[(name, _docs_in(d)) for name, d in group]
            for group in _checkouts.grouped(lambda d: bool(_docs_in(d)))]


def roots() -> dict[str, dict[str, Path]]:
    """{checkout: {kind: folder}} for every folder that exists."""
    out = {name: got for group in checkouts() for name, got in group}
    for name, folder in folders().items():
        if folder.is_dir():
            out.setdefault(name, {"docs": folder})
    return out


def root_for(cwd: str) -> tuple[str, dict[str, Path]] | None:
    """The project a session's working directory is in: the nearest folder
    upward with docs/superpowers (a worktree has its own). The walk ends at
    the outermost folder in ``checkout_dirs``."""
    if not cwd:
        return None
    stops = set(_checkouts.dirs())
    names = {} if stops else {d: n for n, d in _checkouts.candidates()}
    p = Path(cwd)
    for d in (p, *p.parents):
        got = _docs_in(d)
        if got:
            return names.get(d, d.name), got
        if d == d.parent or (d in stops and not any(q in stops for q in d.parents)):
            break
    return None


def title_of(f: Path, mtime: float, size: int) -> str:
    """The file's first heading, or its name; cached by (path, mtime, size)."""
    key = (str(f), mtime, size)
    if key in _titles:
        return _titles[key]
    t = ""
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                m = re.match(r"^#{1,3}\s+(.+?)\s*#*\s*$", line)
                if m:
                    t = m.group(1).strip()
                    break
    except OSError:
        pass
    t = t or re.sub(r"^\d{4}-\d{2}-\d{2}-", "", f.stem).replace("-", " ")
    _titles[key] = t
    return t


def listing(project: str, folders: dict[str, Path], skip: set[tuple[str, str]] | None = None) -> list[dict]:
    """A checkout's documents, less any (kind, name) in `skip`, which is added
    to as it goes: what a group's first checkout has, its worktrees do not
    list again. Named from scandir, so only the files kept are stat-ed: a
    network or Windows drive charges for every stat, and worktrees of a big
    repository hold thousands of copies."""
    out = []
    for kind, folder in folders.items():
        try:
            with os.scandir(folder) as it:
                names = [(e.name, e.path) for e in it if NAME.match(e.name) and not e.is_symlink()]
        except OSError:
            continue
        for name, path in names:
            if skip is not None:
                if (kind, name) in skip:
                    continue
                skip.add((kind, name))
            f = Path(path)
            try:
                st = f.stat()
            except OSError:
                continue
            m = re.match(r"^(\d{4}-\d{2}-\d{2})", name)
            out.append({"id": f"{project}/{kind}/{name}", "project": project, "kind": kind, "name": name,
                        "title": title_of(f, st.st_mtime, st.st_size), "date": m.group(1) if m else "",
                        "modified": st.st_mtime, "bytes": st.st_size})
    return out


def _newest_first(docs: list[dict]) -> list[dict]:
    # The date in the name is when it was written; mtime breaks ties and dates
    # files that have none. Sorting on mtime alone floats an old spec to the
    # top every time someone fixes a typo in it.
    return sorted(docs, key=lambda d: (d["date"] or time.strftime("%Y-%m-%d", time.localtime(d["modified"])),
                                       d["modified"]), reverse=True)


def all_docs(fresh: bool = False) -> list[dict]:
    """Every document, newest first, each with the repository it belongs to
    ("repo": its group's head checkout, or an extra folder's own name)."""
    key = (_checkouts.key(), tuple(sorted(folders().items())))
    with _lock:
        if (not fresh and _cache["docs"] is not None and _cache["key"] == key
                and time.time() - _cache["at"] < SCAN_S):
            return _cache["docs"]
    docs: list[dict] = []
    for group in checkouts():
        skip: set[tuple[str, str]] = set()
        for name, found in group:
            for d in listing(name, found, skip):
                d["repo"] = group[0][0]
                docs.append(d)
    for name, folder in folders().items():
        if folder.is_dir():
            docs += [dict(d, repo=name) for d in listing(name, {"docs": folder})]
    docs = _newest_first(docs)
    with _lock:
        _cache.update(at=time.time(), key=key, docs=docs)
    return docs


def _group_of(checkout: str) -> list[str]:
    for group in checkouts():
        names = [n for n, _ in group]
        if checkout in names:
            return names
    return [checkout]


def for_cwd(cwd: str) -> dict:
    """A session's whole repository, its worktrees included: an agent in one
    checkout writes a spec in another worktree as often as not."""
    got = root_for(cwd)
    if not got:
        return {"ok": True, "project": "", "docs": []}
    group = _group_of(got[0])
    docs = [d for d in all_docs() if d.get("project") in group]
    if not any(d["project"] == got[0] for d in docs):      # not a listed checkout: its own folder only
        docs = _newest_first(listing(got[0], got[1]))
    return {"ok": True, "project": group[0], "docs": docs}


def _resolve(folders: dict[str, Path], kind: str, name: str) -> Path | None:
    folder = folders.get(kind)
    if not folder or not NAME.match(name or ""):
        return None
    return safe_paths.file(folder, name)


def read(folders: dict[str, Path] | None, project: str, kind: str, name: str) -> dict:
    f = _resolve(folders or {}, kind, name)
    if not f:
        return {"ok": False, "detail": "No such document."}
    try:
        st = f.stat()
        with open(f, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return {"ok": False, "detail": f"Could not read it: {exc.strerror or exc}"}
    return {"ok": True, "id": f"{project}/{kind}/{name}", "project": project, "kind": kind, "name": name,
            "title": title_of(f, st.st_mtime, st.st_size), "modified": st.st_mtime,
            "text": raw[:MAX_BYTES].decode("utf-8", errors="replace"), "cut": len(raw) > MAX_BYTES}


def read_id(doc_id: str) -> dict:
    """A document from the all-projects list, by its id ("project/kind/name")."""
    project, _, rest = (doc_id or "").partition("/")
    kind, _, name = rest.partition("/")
    return read(roots().get(project), project, kind, name)


def read_in(cwd: str, kind: str, name: str, project: str = "") -> dict:
    """A document from a session's own repository: its checkout, or another
    checkout of the same repository named by `project`."""
    got = root_for(cwd)
    if not got:
        return {"ok": False, "detail": "This agent's project has no specs folder."}
    if project and project != got[0]:
        if project not in _group_of(got[0]):
            return {"ok": False, "detail": "No such document."}
        return read(roots().get(project), project, kind, name)
    return read(got[1], got[0], kind, name)


def live_by_repo(agents: list[tuple[str, str]]) -> dict[str, list[str]]:
    """{repo: [speaker names]} from (name, cwd) of each live agent, keyed as
    a document's "repo" is: a session in any checkout of a repository counts
    for its head's specs, and a session in an extra folder's parent for that
    folder's."""
    head = {name: group[0][0] for group in checkouts() for name, _ in group}
    extra = [(name, folder.parent.resolve()) for name, folder in folders().items()]
    out: dict[str, list[str]] = {}
    for name, cwd in agents:
        here = Path(cwd).resolve() if cwd else None
        repo = next((n for n, home in extra if here is not None and here.is_relative_to(home)), "")
        if not repo:
            got = root_for(cwd)
            repo = head.get(got[0], got[0]) if got else ""
        if repo:
            out.setdefault(repo, []).append(name)
    return {k: sorted(v) for k, v in out.items()}


# --- the push ---------------------------------------------------------------

def news(docs: list[dict], seen: set[str] | None) -> tuple[list[dict], set[str]]:
    """(the new specs to push, the ids now seen). Plans are listed but not
    pushed: an agent writes one and starts on it in the same breath, while a
    spec is what waits for a read. None seen means first sight: silent."""
    ids = {d["id"] for d in docs}
    if seen is None:
        return [], ids
    return [d for d in docs if d["id"] not in seen and d["kind"] in ("specs", "docs")], ids | seen


def message(new: list[dict], speakers: dict[str, list[str]]) -> tuple[str, str, str] | None:
    """(title, body, doc id or '') for one push. `speakers` is who is live in
    each project, so the push can say who wrote it when only one could have."""
    if not new:
        return None
    if len(new) > BURST:
        projects = sorted({d["project"] for d in new})
        return (f"{len(new)} new specs", "In " + ", ".join(projects) + ". Tap to read them.", "")
    d = new[0]
    who = speakers.get(d.get("repo") or d["project"]) or []
    by = f"{who[0]} wrote it, in {d['project']}." if len(who) == 1 else f"In {d['project']}."
    more = f" And {len(new) - 1} more." if len(new) > 1 else ""
    return (f"New spec: {d['title']}", by + more, d["id"] if len(new) == 1 else "")


class Watcher:
    """send(title, body, url, tag) -> delivered; speakers() -> live_by_repo."""

    def __init__(self, send, speakers, state: Path | None = None):
        self.send, self.speakers, self.state = send, speakers, Path(state or seen_file())

    def _seen(self) -> set[str] | None:
        try:
            return set(json.loads(self.state.read_text()).get("seen") or [])
        except (OSError, ValueError, AttributeError):
            return None

    def _save(self, seen: set[str]) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": sorted(seen)}))
        os.replace(tmp, self.state)

    def tick(self) -> None:
        docs = all_docs(fresh=True)
        if not docs:
            return                       # a drive not answering is not every spec deleted
        new, seen = news(docs, self._seen())
        msg = message(new, self.speakers() if new else {})
        if msg:
            title, body, doc = msg
            url = "/?view=specs" + (f"&doc={quote(doc)}" if doc else "")
            if not self.send(title, body, url, f"spec-{doc or 'many'}"):
                return                   # not delivered: try again next round
        self._save(seen)

    def start(self) -> None:
        if "pytest" in sys.modules:
            return

        def loop():
            while True:
                try:
                    self.tick()
                except Exception:
                    pass
                time.sleep(PUSH_EVERY_S)

        threading.Thread(target=loop, name="specs-push", daemon=True).start()
