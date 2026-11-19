"""Git state that never makes a page wait on `git status`.

GitState caches against a repo's .git/index, so a repo whose agent just ran
git is cold again, and the next read shells out: 0.4 to 6 s a checkout on a
Windows drive under WSL (measured 2026-11-09, the whole cost of a chat
window's first open).

`LiveGit.state(cwd)` gives the fresh answer when it comes within `wait`
seconds, else the last one this repo gave (None if it never answered) while
the refresh finishes in the background; the page's next poll has it. One
refresh per folder at a time, so a slow repo is never run twice over.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout

WAIT_S = 0.25


class LiveGit:
    def __init__(self, git, wait: float | None = WAIT_S, workers: int = 2) -> None:
        self.git = git                  # anything with for_repo(cwd)
        self.wait = wait                # None: block, as a bare for_repo would
        self._workers = workers
        self._pool: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._running: dict[str, Future] = {}
        self._last: dict[str, dict | None] = {}

    def _refresh(self, key: str, cwd) -> dict | None:
        try:
            got = self.git.for_repo(cwd)
        except Exception:               # a lock or a timeout: keep the last answer
            with self._lock:
                self._running.pop(key, None)
                return self._last.get(key)
        with self._lock:
            self._last[key] = got
            self._running.pop(key, None)
        return got

    def state(self, cwd, wait: float | None = -1) -> dict | None:
        key = str(cwd or "")
        if not key:
            return None
        wait = self.wait if wait == -1 or self.wait is None else wait
        if wait is None:
            return self._refresh(key, cwd)
        with self._lock:
            fut = self._running.get(key)
            if fut is None:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(self._workers, thread_name_prefix="gitlive")
                fut = self._pool.submit(self._refresh, key, cwd)
                self._running[key] = fut
            last = self._last.get(key)
        try:
            return fut.result(timeout=wait)
        except FutureTimeout:
            return last
        except Exception:
            return last
