"""Private HTML pages an agent can publish for the dashboard.

The HTTP router authenticates callers.  This module owns the durable records,
the source-folder privacy rule, and the deliberately narrow visibility rule so
tests can exercise them without a live server or push service.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config


DEFAULT_DIR = Path.home() / ".local" / "share" / "agent-companion" / "pages"
MAX_BYTES = 5 * 1024 * 1024
OWNER_ID = "owner"
_PAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# This is intentionally a complete, explicit response policy.  In particular
# it has no allow-same-origin: the page receives an opaque origin, not the
# dashboard's cookies, storage, routes, or native bridge.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms; "
        "default-src 'none'; "
        "script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net "
        "https://unpkg.com https://cdn.tailwindcss.com https://code.jquery.com; "
        "style-src 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net "
        "https://unpkg.com https://cdnjs.cloudflare.com; "
        "font-src https://fonts.gstatic.com data:; img-src data: blob:; media-src data: blob:; "
        "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Type": "text/html; charset=utf-8",
}


class Refused(ValueError):
    """A syntactically valid publish request that is not allowed."""


def page_dir() -> Path:
    """The pages root, late-bound so tests never use a live store."""
    configured = str(config.get("pages_dir") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DIR


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def private_dirs() -> tuple[Path, ...]:
    """Configured source folders which must remain owner-only."""
    return tuple(_absolute(path) for path in config.paths("pages_private_dirs"))


def is_private_folder(folder: str | Path, dirs: Iterable[str | Path] | None = None) -> bool:
    """Whether *folder* is in a folder explicitly kept out of shared pages."""
    here = _absolute(folder)
    roots = private_dirs() if dirs is None else tuple(_absolute(path) for path in dirs)
    return any(_inside(here, root) for root in roots)


def can_see(record: dict[str, Any], viewer: str | None) -> bool:
    """The owner sees all pages; a known shared person sees shared ones only."""
    if not viewer:
        return False
    return str(record.get("owner") or "") == viewer or bool(record.get("household"))


def page_summary(record: dict[str, Any]) -> dict[str, Any]:
    """The list card deliberately excludes the local source path."""
    return {key: record.get(key) for key in (
        "id", "title", "owner", "household", "speaker", "project",
        "created", "updated", "bytes",
    )}


class Store:
    """One outside-the-repository directory per page.

    owner_for, household_refusal, and visible let a host customize ownership, refusal, and visibility.
    They default to None, retaining the default owner, private-folder refusal, and can_see policy.
    """

    def __init__(self, root: str | Path | None = None, *, now: Callable[[], float] = time.time,
                 token: Callable[[int], str] = secrets.token_urlsafe,
                 private: Iterable[str | Path] | None = None,
                 owner_for: Callable[[Path], str] | None = None,
                 household_refusal: Callable[[Path, Path, str], str | None] | None = None,
                 visible: Callable[[dict, Any], bool] | None = None):
        self.root = Path(root) if root is not None else page_dir()
        self.now = now
        self.token = token
        self.private = tuple(_absolute(path) for path in private) if private is not None else None
        self.owner_for = owner_for
        self.household_refusal = household_refusal
        self.visible = visible

    def _root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _folder(self, page_id: str) -> Path | None:
        return self.root / page_id if _PAGE_ID.fullmatch(page_id or "") else None

    @staticmethod
    def _write(path: Path, text: str) -> None:
        # Repair permissions when replacing a page too, even if a backup or
        # permissive umask changed them.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.chmod(path, 0o600)

    def _records(self):
        try:
            folders = list(self.root.iterdir())
        except OSError:
            return
        for folder in folders:
            if not folder.is_dir() or not _PAGE_ID.fullmatch(folder.name):
                continue
            record = self._read_meta(folder)
            if record is not None:
                yield record

    @staticmethod
    def _read_meta(folder: Path) -> dict[str, Any] | None:
        try:
            record = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return record if isinstance(record, dict) and isinstance(record.get("id"), str) else None

    def get(self, page_id: str) -> dict[str, Any] | None:
        folder = self._folder(page_id)
        if folder is None:
            return None
        record = self._read_meta(folder)
        return record if record is not None and record.get("id") == page_id else None

    def html(self, page_id: str) -> str | None:
        folder = self._folder(page_id)
        if folder is None or self.get(page_id) is None:
            return None
        try:
            return (folder / "page.html").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def can_view(self, record: dict[str, Any], viewer: Any) -> bool:
        """The one visibility rule for every read: the host's visible hook, else can_see."""
        return (self.visible or can_see)(record, viewer)

    def list_for(self, viewer: Any) -> list[dict[str, Any]]:
        records = [page_summary(record) for record in self._records() if self.can_view(record, viewer)]
        return sorted(records, key=lambda record: float(record.get("updated") or 0), reverse=True)

    def _find_source(self, owner: str, source: str) -> dict[str, Any] | None:
        return next((record for record in self._records()
                     if record.get("owner") == owner and record.get("source") == source), None)

    def publish(self, *, html: str, title: str, folder: str | Path, source: str | Path,
                speaker: str, project: str, household: bool,
                pusher: Callable[[str, str, str, str, str], Any] | None = None) -> tuple[dict[str, Any], bool]:
        """Store a page and return ``(record, created)``.

        The publisher cannot select an owner: this core's hook token belongs
        to its owner.  A shared page is visible only to identities an add-on
        has already authenticated through the core's person mechanism.
        """
        if not isinstance(html, str):
            raise Refused("page HTML must be text")
        size = len(html.encode("utf-8"))
        if size > MAX_BYTES:
            raise Refused(f"page is {size} bytes; the limit is {MAX_BYTES} bytes")
        if not isinstance(title, str) or not title.strip():
            raise Refused("page needs a title")
        if not isinstance(folder, (str, Path)) or not str(folder):
            raise Refused("page needs its caller folder")
        if not isinstance(source, (str, Path)) or not str(source):
            raise Refused("page needs its source path")

        folder_path = _absolute(folder)
        source_path = _absolute(source)
        owner = self.owner_for(folder_path) if self.owner_for is not None else OWNER_ID
        if self.household_refusal is None:
            protected = self.private if self.private is not None else private_dirs()
            if household and (is_private_folder(folder_path, protected) or is_private_folder(source_path, protected)):
                raise Refused("pages from pages_private_dirs cannot be household pages")
        elif household:
            refusal = self.household_refusal(folder_path, source_path, owner)
            if refusal:
                raise Refused(refusal)

        self._root()
        existing = self._find_source(owner, str(source_path))
        stamp = self.now()
        created = existing is None
        if existing is None:
            while True:
                page_id = self.token(12)
                page_folder = self.root / page_id
                try:
                    page_folder.mkdir(mode=0o700)
                    break
                except FileExistsError:
                    continue
            os.chmod(page_folder, 0o700)
            record: dict[str, Any] = {"id": page_id, "created": stamp}
        else:
            record = existing
            page_folder = self.root / str(record["id"])
            os.chmod(page_folder, 0o700)

        record.update({
            "title": title.strip(), "owner": owner, "household": bool(household),
            "speaker": str(speaker or "an agent"), "project": str(project or folder_path.name),
            "source": str(source_path), "updated": stamp, "bytes": size,
        })
        self._write(page_folder / "page.html", html)
        self._write(page_folder / "meta.json", json.dumps(record, sort_keys=True, separators=(",", ":")))

        if created and pusher is not None:
            pusher(owner, f"Page from {record['speaker']} ({record['project']})", record["title"],
                   f"/?view=pages&page={record['id']}", "pages")
        return dict(record), created

    def delete(self, page_id: str, viewer: str | None) -> bool:
        record = self.get(page_id)
        if record is None or not viewer or record.get("owner") != viewer:
            return False
        folder = self._folder(page_id)
        if folder is None:
            return False
        try:
            shutil.rmtree(folder)
        except OSError:
            return False
        return True
