"""The merge queue: how work from the Claudes on one repository lands on its
base branch.

Every job happens in its own worktree on its own branch. A session whose own
targeted tests pass runs `python -m agent_companion.mergeq add` and moves on:
the branch is queued, pinned to the sha it was queued at. One merger, in code
and never a model (`mergeq run`, by hand or the systemd timer), takes the queue
in batches: it merges the entries in an integration worktree of its own, runs
the whole suite ONCE, and on red finds the entry that turned it red by testing
each one alone on the base, ejects it with the reason and tests the rest again.
On green it lands them, one of two ways (`mergeq_land`):

- "pr" (the default): each branch is pushed and gets a PR at the tested sha,
  merged once its required checks pass (`gh pr merge --match-head-commit`); a
  PR queued with `add --pr` is merged the same way. The base branch moves only
  through GitHub; the live checkout then fast-forwards to it.
- "local": the base branch is fast-forwarded to the tested commit in the live
  checkout, which must be on it with a clean tracked tree, and pushed.

It never resolves a conflict, never edits code and never forces. Stdlib only.
Every outside thing (the suite, the push, gh, the restart, the notices, a hold
on the machine's other work while the suite runs) is a field of Merger, so a
host app plugs in its own and the tests touch nothing real. State lives in one
directory (AGENT_COMPANION_MERGEQ_DIR, default
~/.local/share/agent-companion/mergeq).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import config

SUITE = "python3 -m pytest -q -x"
UNIT = "agent-companion"
HISTORY = 40
QUEUED, MERGED, CONFLICT, FAILED, STALE, PUSH_FAILED = (
    "queued", "merged", "conflict", "failed", "stale", "push-failed")


def state_dir() -> Path:
    return Path(config.env("MERGEQ_DIR")
                or Path.home() / ".local" / "share" / "agent-companion" / "mergeq")


def land_mode() -> str:
    """How a green batch lands: "pr" (the default) or "local"."""
    mode = str(config.get("mergeq_land") or "pr")
    return mode if mode in ("pr", "local") else "pr"


def base_branch() -> str:
    """The branch the queue lands on (`mergeq_base`, default main)."""
    return str(config.get("mergeq_base") or "main")


def _env() -> dict:
    e = dict(os.environ)
    for k, v in (("GIT_AUTHOR_NAME", "mergeq"), ("GIT_COMMITTER_NAME", "mergeq"),
                 ("GIT_AUTHOR_EMAIL", "mergeq@localhost"), ("GIT_COMMITTER_EMAIL", "mergeq@localhost")):
        e.setdefault(k, v)
    e["GIT_TERMINAL_PROMPT"] = "0"
    return e


def _run(args, cwd=None, timeout=None, shell=False):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=_env(),
                          stdin=subprocess.DEVNULL, timeout=timeout, shell=shell)


class GitError(RuntimeError):
    pass


class _Stalled(Exception):
    pass


class _BaseMoved(Exception):
    """The base branch on origin is not what the queue tested on: nothing more lands this batch."""


def git(cwd, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and r.returncode:
        raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


def _ok(cwd, *args) -> bool:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True).returncode == 0


def main_checkout(start: Path | None = None) -> Path:
    """The primary checkout: the first entry of `git worktree list`."""
    start = Path(start or config.env("MERGEQ_REPO") or ".")
    out = git(start, "worktree", "list", "--porcelain")
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line[9:])
    raise GitError("not inside a git repository")


# --- the entries ---------------------------------------------------------------

class Store:
    """One JSON file per entry, written under a flock; `last.json` is the
    previous batch's result and `history.json` the newest outcomes."""

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory or state_dir())
        self.dir.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def _lock(self, name: str):
        f = open(self.dir / name, "a+")
        return f

    def locked(self, name: str = ".lock", blocking: bool = True):
        """Context manager for a flock; yields False when not blocking and busy."""
        store = self

        class _L:
            def __enter__(s):
                s.f = store._lock(name)
                try:
                    fcntl.flock(s.f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                    return True
                except BlockingIOError:
                    return False

            def __exit__(s, *a):
                try:
                    fcntl.flock(s.f, fcntl.LOCK_UN)
                finally:
                    s.f.close()
        return _L()

    def entries(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("[0-9]*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, ValueError):
                continue
        return out

    def queue(self) -> list[dict]:
        """The entries still waiting, oldest first."""
        return [e for e in self.entries() if e["state"] == QUEUED]

    def save(self, e: dict) -> None:
        p = self.dir / f"{e['id']}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(e, indent=1))
        os.replace(tmp, p)

    def mark(self, e: dict) -> None:
        """Rewrite an entry under the same lock add() takes, and only if it is
        still there (add() may have replaced it with a newer sha meanwhile)."""
        with self.locked():
            if (self.dir / f"{e['id']}.json").exists():
                self.save(e)

    def throttled(self, kind: str, every_s: float, now: float) -> bool:
        """True when `kind` was already announced within `every_s`; records it otherwise."""
        with self.locked():
            p = self.dir / "announced.json"
            try:
                seen = json.loads(p.read_text())
            except (OSError, ValueError):
                seen = {}
            if now - seen.get(kind, 0) < every_s:
                return True
            seen[kind] = now
            p.write_text(json.dumps(seen))
            return False

    def waiting_since(self, why: Optional[str], now: float) -> float:
        """When the queue started waiting (kept while it keeps waiting), 0 when it is not."""
        with self.locked():
            p = self.dir / "waiting.json"
            if why is None:
                p.unlink(missing_ok=True)
                return 0
            try:
                since = json.loads(p.read_text()).get("since", now)
            except (OSError, ValueError):
                since = now
            p.write_text(json.dumps({"since": since, "why": why}))
            return since

    def add(self, branch: str, sha: str, summary: str = "", pr: int | None = None, *,
            speaker: str = "", session: str = "") -> dict:
        with self.locked():
            for e in self.entries():       # one live entry per branch: the newest sha wins
                if e["branch"] == branch and e["state"] in (QUEUED, STALE, CONFLICT, FAILED, PUSH_FAILED):
                    (self.dir / f"{e['id']}.json").unlink(missing_ok=True)
            e = self._entry(branch, sha, summary, pr, speaker, session, time.time())
            self.save(e)
            return e

    @staticmethod
    def _entry(branch, sha, summary, pr, speaker, session, added, ns: int | None = None) -> dict:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)
        e = {"id": f"{time.time_ns() if ns is None else ns}-{slug}", "branch": branch, "sha": sha, "speaker": speaker,
             "session": session, "summary": summary, "state": QUEUED, "added": added}
        if pr is not None:
            e["pr"] = pr
        return e

    def _migrate(self) -> None:
        """A queue written before each entry had a file of its own (queue.json,
        one list): each waiting entry becomes its file, once, and the list is
        kept beside them as queue.json.migrated."""
        old = self.dir / "queue.json"
        if not old.exists():
            return
        with self.locked():
            if not old.exists():
                return
            try:
                q = json.loads(old.read_text())
            except (OSError, ValueError):
                q = []
            for i, e in enumerate(q if isinstance(q, list) else []):
                if isinstance(e, dict) and e.get("branch") and e.get("sha"):
                    added = e.get("added")
                    added = added if isinstance(added, (int, float)) and math.isfinite(added) else 0.0
                    # The id comes from the old entry, so a migration cut short and run again
                    # rewrites the same files instead of queueing an entry twice.
                    self.save(self._entry(e["branch"], e["sha"], e.get("summary", ""), e.get("pr"), "", "",
                                          added, ns=int(added * 1e9) + i))
            os.replace(old, old.with_name("queue.json.migrated"))

    def last(self) -> Optional[dict]:
        try:
            return json.loads((self.dir / "last.json").read_text())
        except (OSError, ValueError):
            return None

    def history(self) -> list[dict]:
        try:
            h = json.loads((self.dir / "history.json").read_text())
        except (OSError, ValueError):
            return []
        return h if isinstance(h, list) else []

    def set_last(self, result: dict) -> None:
        for name, data in (("last.json", result),
                           ("history.json", (self.history() + result.get("outcomes", []))[-HISTORY:])):
            p = self.dir / name
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            os.replace(tmp, p)

    def log(self, msg: str) -> None:
        with open(self.dir / "mergeq.log", "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


# --- the suite -----------------------------------------------------------------

# The test a failure line names: tests/x.py::test_y, with its [params] when they hold no space. A parametrized
# id can hold spaces, which a plain \S+ id would not match at all, and then every red looks unexplained.
# Rerunning the plain test runs each of its parameters, the failed ones among them.
FAIL_RE = re.compile(r"^(?:FAILED|ERROR) (tests/[\w/.-]+\.py(?:::\w+(?:\[[^\]\s]*\](?=\s|$))?)?)", re.M)
FAIL_LINE_RE = re.compile(r"^(?:FAILED|ERROR) \S+.*$", re.M)


def failing_ids(output: str) -> list[str]:
    return sorted(set(FAIL_RE.findall(output)))


SUMMARY_RE = re.compile(r"^=*\s*(?P<body>(?:\d+ \w+(?: \(.*?\))?,? ?)+)\s+in [\d.]+s", re.M)


def parse_summary(output: str) -> Optional[dict]:
    """pytest's last summary line as {word: count}, or None when there is none."""
    found = None
    for m in SUMMARY_RE.finditer(output):
        found = {w: int(n) for n, w in re.findall(r"(\d+) (\w+)", m.group("body"))}
    return found


def _passed_text(passed: int, res: dict) -> str:
    """What a landed entry says about the batch tests that passed."""
    text = f"{passed} passed" if passed else ""
    rerun = res.get("flaky_rerun")
    ids = rerun.get("ids") if isinstance(rerun, dict) else []
    count = len(ids) if isinstance(ids, list) else 0
    if count:
        rerun_text = f"{count} that failed once passed on a rerun"
        return f"{text}, and {rerun_text}" if text else rerun_text
    return text


def green_by_the_book(returncode: int, output: str) -> bool:
    """Explicit pass only: exit 0 AND a parsed summary with passed > 0 and no
    failed or error count. A crash, timeout, kill, collection error, empty run
    or output we cannot parse is red."""
    summary = parse_summary(output)
    return (returncode == 0 and summary is not None and summary.get("passed", 0) > 0
            and not summary.get("failed") and not summary.get("error") and not summary.get("errors"))


def suite_tail(output: str) -> str:
    """What a batch keeps of a run: every failure line first, each with its
    message (warnings can fill the end, and a flake that passes on the rerun
    leaves only this behind), then the last 1,500 characters."""
    ids = failing_ids(output)
    head = ("\n".join(m.group(0)[:300] for m in FAIL_LINE_RE.finditer(output))
            or "\n".join(f"FAILED {i}" for i in ids[:40]))
    return (head + "\n...\n" if head else "") + output[-1500:]


def run_command(tree: Path, ids: Optional[list[str]] = None, command: str = SUITE,
                timeout: float = 3600) -> tuple[bool, list[str], str]:
    """`command` in `tree`, with `ids` appended when given -> (green, failing
    ids, output tail). Anything but an explicit pass is red, an exception included."""
    cmd = command + "".join(" " + shlex.quote(i) for i in (ids or []))
    try:
        r = _run(cmd, cwd=str(tree), timeout=timeout, shell=True)
    except Exception as exc:       # timeout, cannot start, killed
        return False, [], f"the suite did not finish: {exc!r}"
    out = (r.stdout or "") + (r.stderr or "")
    return green_by_the_book(r.returncode, out), failing_ids(out), suite_tail(out)


# --- gh, for land = "pr" ----------------------------------------------------------

CHECKS_WAIT_S = 30 * 60
NO_CHECKS_GRACE_S = 90     # a PR opened a moment ago may not have its checks registered yet


def gh_ready(gh: str = "gh") -> str:
    """'' when gh is installed and signed in, else the reason."""
    if not shutil.which(gh):
        return "gh is not installed (https://cli.github.com); nothing changed"
    try:
        r = _run([gh, "auth", "status"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "gh did not answer; nothing changed"
    return "" if r.returncode == 0 else "gh is not signed in (run `gh auth login`); nothing changed"


def _pr_view(repo, gh: str, number: int, fields: str) -> tuple[dict | None, str]:
    """Return selected PR fields, or a user-facing problem."""
    try:
        r = _run([gh, "pr", "view", str(number), "--json", fields], cwd=repo, timeout=120)
    except (OSError, subprocess.SubprocessError) as ex:
        return None, f"gh pr view: {ex}"
    if r.returncode:
        return None, "gh pr view: " + _last_line(r.stderr or r.stdout)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return None, "gh pr view: unreadable answer"
    if not isinstance(data, dict):
        return None, "gh pr view: unreadable answer"
    return data, ""


def _await_checks(repo, gh, number) -> str:
    """'' once the PR's required checks pass, or when the branch requires none.
    The base branch may require CI, and on a PR opened a moment ago it has only
    just started, so GitHub refuses the merge."""
    give_up = time.time() + NO_CHECKS_GRACE_S
    while True:
        try:
            r = _run([gh, "pr", "checks", str(number), "--required", "--watch", "--interval", "20"],
                     cwd=repo, timeout=CHECKS_WAIT_S)
        except subprocess.TimeoutExpired:
            return f"PR #{number}: required checks still running after {CHECKS_WAIT_S // 60} min; not merged"
        if not r.returncode:
            return ""
        said = (r.stdout or "") + (r.stderr or "")
        if "no required checks" not in said and "no checks reported" not in said:
            return f"PR #{number}: required checks did not pass ({_last_line(said)}); not merged"
        # None yet: either the branch requires none, or GitHub has not started
        # them. Wait out the grace, then let the merge itself say which.
        if time.time() >= give_up:
            return ""
        time.sleep(15)


def _open_pr(repo, gh, e, passed, base="main") -> tuple[Optional[int], str]:
    """The entry's open PR onto `base` at the tested sha, opened now or found
    already open: (its number, '') or (None, the problem)."""
    b, sha = e["branch"], e["sha"]
    body = (e.get("summary") or b) + f"\n\nTests: {passed} passed on the integration run"
    title = " ".join((e.get("summary") or "").split())[:72] or _subject(repo, sha or b) or b
    c = _run([gh, "pr", "create", "--head", b, "--base", base, "--title", title, "--body", body],
             cwd=repo, timeout=120)
    if c.returncode and "already exists" not in (c.stderr or ""):
        return None, "gh pr create: " + _last_line(c.stderr)
    m = re.search(r"/pull/(\d+)", c.stdout or "")
    want = int(m.group(1)) if m else None
    lst = _run([gh, "pr", "list", "--head", b, "--base", base, "--state", "open", "--json",
                "number,headRefOid,headRepositoryOwner,isCrossRepository,baseRefName"], cwd=repo, timeout=120)
    if lst.returncode:
        return None, "gh pr list: " + _last_line(lst.stderr)
    try:
        prs = [x for x in json.loads(lst.stdout or "[]")
               if (want is None or x.get("number") == want) and x.get("baseRefName") == base]
    except (AttributeError, ValueError):
        return None, "gh pr list: unreadable answer"
    if len(prs) != 1:
        return None, f"gh pr list: expected one open PR for {b} onto {base}, found {len(prs)}"
    pr = prs[0]
    if pr.get("headRefOid") != sha:
        return None, f"PR #{pr.get('number')} head is {str(pr.get('headRefOid'))[:12]}, not the tested {sha[:12]}; not merged"
    if pr.get("isCrossRepository"):
        return None, f"PR #{pr.get('number')} comes from another repository; not merged"
    return pr["number"], ""


def _pr_target(repo, gh, number, base) -> str:
    """Why the PR must not merge now because it targets another branch than
    the one the queue tested on (retargeted since it was opened or queued), or ''."""
    pr, problem = _pr_view(repo, gh, number, "baseRefName")
    if not pr:
        return problem
    target = pr.get("baseRefName")
    return "" if target == base else f"PR #{number} targets {target or 'no branch'}, not {base}; not merged"


MERGE_CONFIRM_S = 5        # a merge gh says it made but the PR does not show yet, looked at once more after this


def _merge_pr(repo, gh, number, sha) -> tuple[str, str]:
    """Merge the PR at the tested sha and take GitHub's word for it: ('', the
    merge commit) once the PR says MERGED, else (the problem, ''). gh's exit
    proves nothing either way: it can fail after merging (its cleanup) and
    succeed without merging (GitHub's own merge queue, or auto-merge, took it)."""
    r = _run([gh, "pr", "merge", str(number), "--merge", "--match-head-commit", sha], cwd=repo, timeout=120)
    fields = "state,mergeCommit,headRefOid,isCrossRepository"
    pr, view_problem = _pr_view(repo, gh, number, fields)
    if not r.returncode and pr and pr.get("state") == "OPEN":
        time.sleep(MERGE_CONFIRM_S)
        pr, view_problem = _pr_view(repo, gh, number, fields)
    if pr and pr.get("state") == "MERGED":
        commit = pr.get("mergeCommit")
        return "", str(commit.get("oid") or "") if isinstance(commit, dict) else ""
    if not r.returncode:
        return (f"PR #{number} was accepted but did not merge ({(pr or {}).get('state') or view_problem}): "
                "GitHub's own merge queue or auto-merge has it, which this queue does not manage; left to GitHub"), ""
    if pr:
        if pr.get("state") != "OPEN":
            return f"PR #{number} is {pr.get('state')}, no longer open; not merged", ""
        if pr.get("isCrossRepository"):
            return f"PR #{number} comes from another repository; not merged", ""
        head = pr.get("headRefOid")
        if head != sha:
            return (f"PR #{number} head moved since it was pinned ({str(head)[:12]}, not {sha[:12]}); "
                    "not merged because --match-head-commit protects the tested commit"), ""
    return "gh pr merge: " + _last_line(r.stderr) + (f" ({view_problem})" if view_problem else ""), ""


def _subject(repo, rev) -> str:
    """The commit's subject line, for a PR title when the queue has no summary."""
    c = _run(["git", "log", "-1", "--format=%s", str(rev)], cwd=repo, timeout=30)
    return (c.stdout or "").strip()[:72] if not c.returncode else ""


def _last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1][:200] if lines else ""


def _note_for_partner(e: dict, notify) -> None:
    m = re.match(r"for-([\w-]+):\s*(.*)", e.get("summary", ""), re.S)
    if not m:
        return
    try:
        notify("for-" + m.group(1), f"Merged: {e['branch']}", m.group(2).strip() or e["branch"])
    except Exception:       # a note is a courtesy; never fail a merge over it
        pass


def _default_notify(label: str, title: str, body: str) -> None:
    from agent_companion import collab
    if collab.label() and collab.repo():
        collab.file(label, title, body, collab.repo())


def _restart(unit: str = UNIT):
    """Restart the app's own service when it has one: the result, or None."""
    if not shutil.which("systemctl"):
        return None
    try:
        if _run(["systemctl", "--user", "cat", unit], timeout=10).returncode:
            return None
        return _run(["systemctl", "--user", "restart", unit], timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None


def _fast_forward(repo: Path, base: str = "main") -> str:
    if git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False) != base:
        return f"main checkout not on {base}; left alone"
    if git(repo, "status", "--porcelain", "--untracked-files=no", check=False):
        return "main checkout has changes; left alone"
    r = _run(["git", "-C", str(repo), "merge", "--ff-only", "-q", f"origin/{base}"])
    return "main checkout fast-forwarded" if r.returncode == 0 else "main checkout could not fast-forward"


# --- the merger ----------------------------------------------------------------

@dataclass
class Merger:
    repo: Path = Path(".")
    store: Store = field(default_factory=Store)
    tree: Path = field(default_factory=lambda: state_dir() / "int")
    suite: Callable = run_command          # (tree, ids or None) -> (green, failing ids, output tail)
    push: Optional[Callable] = None        # (repo) -> result; None pushes the base branch to origin
    restart: Callable = _restart           # () -> result with a returncode, or None
    notify: Callable = lambda title, body: None
    handoff: Callable = lambda entry, text: None
    hold: Callable = lambda on: None       # the machine's other work, held while the suite runs
    budget_s: float = 1.7 * 3600           # inside the unit's TimeoutStartSec=2h
    base_branch: str = "main"
    now: Callable = time.time
    land: str = "local"                    # "local" or "pr"
    gh: str = "gh"
    partner: Callable = lambda label, title, body: None    # a "for-<label>:" summary's note
    identity: tuple = ("mergeq", "mergeq@localhost")       # the merge commits' author
    file_mode: bool = False                # the tree takes git's executable bits (a Linux tree of a Windows checkout)
    note: dict = field(default_factory=dict)               # what the suite says about how it ran, kept in the result
    note_key: str = "suite_note"

    # -- plumbing --
    def _base_ref(self) -> str:
        return f"origin/{self.base_branch}" if self.land == "pr" else self.base_branch

    def _fresh_tree(self, at: str) -> None:
        self._drop_tree()
        git(self.repo, "worktree", "prune")
        git(self.repo, "worktree", "add", "--detach", str(self.tree), at)
        if self.file_mode:
            git(self.repo, "config", "extensions.worktreeConfig", "true")
            git(self.tree, "config", "--worktree", "core.fileMode", "true")

    def _drop_tree(self) -> None:
        if self.tree.exists():
            git(self.repo, "worktree", "remove", "--force", str(self.tree), check=False)
            shutil.rmtree(self.tree, ignore_errors=True)
        git(self.repo, "worktree", "prune", check=False)

    def _merge(self, e: dict) -> Optional[list[str]]:
        """Merge one entry into the tree. None when it went in, else the files in conflict."""
        name, email = self.identity
        r = subprocess.run(["git", "-C", str(self.tree), "-c", f"user.name={name}", "-c", f"user.email={email}",
                            "merge", "--no-ff", "-m", f"Merge {e['branch']} (merge queue)", e["sha"]],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
                                "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode == 0:
            return None
        files = git(self.tree, "diff", "--name-only", "--diff-filter=U", check=False).split()
        git(self.tree, "merge", "--abort", check=False)
        git(self.tree, "reset", "--hard", "HEAD", check=False)
        return files or ["(merge failed: " + (r.stderr or r.stdout).strip()[:200] + ")"]

    def _master_ready(self) -> Optional[str]:
        """Why the base branch cannot be moved in the live checkout now, or None."""
        head = git(self.repo, "symbolic-ref", "-q", "HEAD", check=False)
        if head != f"refs/heads/{self.base_branch}":
            return f"the live checkout is not on {self.base_branch}"
        if git(self.repo, "status", "--porcelain", "--untracked-files=no", check=False):
            return f"{self.base_branch}'s working tree has uncommitted changes"
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD",
                       "REVERT_HEAD", "BISECT_LOG"):
            path = git(self.repo, "rev-parse", "--git-path", marker, check=False)
            if path and (Path(path) if Path(path).is_absolute() else self.repo / path).exists():
                return f"a {marker} is in progress in the live checkout"
        return None

    def _hold(self, on: bool) -> None:
        self.hold(on)

    def _push(self):
        if self.push is not None:
            return self.push(self.repo)
        return _run(["git", "-C", str(self.repo), "push", "origin", self.base_branch])

    # -- one batch --
    def run(self) -> dict:
        with self.store.locked(".run.lock", blocking=False) as got:
            if not got:
                return {"result": "busy", "detail": "another batch is running"}
            self._res: dict = {}
            self._held = False
            try:
                master0 = git(self.repo, "rev-parse", self._base_ref(), check=False)
            except Exception:
                master0 = ""
            try:
                result = self._run()
            except Exception as exc:       # fail closed: whatever happened, say so
                # In pr mode origin's base is only known by a fetch, so what landing recorded counts first.
                moved = bool(self._res.get("master_moved")) or \
                    git(self.repo, "rev-parse", self._base_ref(), check=False) not in ("", master0)
                result = {"at": self.now(), "result": "failed", "merged": list(self._res.get("merged", [])),
                          "conflict": [], "failed": [], "stale": [], "pushed": self._res.get("pushed"),
                          "master_moved": moved, "outcomes": list(self._res.get("outcomes", [])),
                          "detail": (f"the batch crashed AFTER {self.base_branch} moved (pushed: "
                                     f"{self._res.get('pushed')}): {exc!r}"
                                     if moved else f"the batch crashed; {self.base_branch} did not move: {exc!r}")[:400]}
                self._alert("crash", f"Merge queue crashed. {result['detail']}")
            finally:
                try:
                    self._drop_tree()
                except Exception:
                    pass
                if self._held:
                    try:
                        self._hold(False)
                    except Exception:
                        pass
            self.store.set_last(result)
            return result

    def _alert(self, kind: str, text: str, every_s: float = 3600) -> None:
        """A notice, at most once per `every_s` per kind, never fatal."""
        try:
            if not self.store.throttled(kind, every_s, self.now()):
                self.notify("Merge queue", text)
        except Exception:
            pass

    def _run(self) -> dict:
        res = self._res = {"at": self.now(), "result": "empty", "merged": [], "conflict": [], "failed": [],
                           "stale": [], "pushed": None, "detail": "", "outcomes": []}
        self._deadline = self.now() + self.budget_s
        if self.land == "pr":
            git(self.repo, "fetch", "-q", "origin")
        self.settle()
        queue = [e for e in self.store.entries() if e["state"] == QUEUED]
        live = []
        for e in queue:
            why = self._moved(e)
            if why:
                self._mark(e, STALE, why)
                res["stale"].append(e["id"])
            else:
                live.append(e)
        if not live:
            self.store.waiting_since(None, self.now())
            return res
        base = res["base"] = git(self.repo, "rev-parse", self._base_ref())
        todo = self._build(base, live, res)
        if not todo:
            res["result"] = "nothing to merge" if (res["conflict"] or res["failed"]) else "empty"
            self._tell(res, live)
            return res
        first_tree = git(self.tree, "rev-parse", "HEAD^{tree}")
        self.note.clear()
        green, bad, tail = self._suite(None)
        res["suite_tail"] = tail
        if not green and not bad:
            # RED WITH NO TEST NAMED is a crash, a kill or a starved machine, not
            # a verdict: testing each entry alone would cost an hour to learn
            # nothing. Once more, whole, before anyone is suspected.
            res["retried"] = True
            green, bad, tail = self._suite(None)
            res["suite_tail"] = tail
        if self.note:
            res[self.note_key] = dict(self.note)
        first_tail, first_note = tail, dict(self.note)
        if not green:
            res["red_tail"] = tail          # kept: a green rerun overwrites suite_tail
            todo = self._find_culprits(base, todo, bad, res)       # sets res["result"] when it cannot go on
            if res["result"] in ("master_red", "stalled", "red") or not todo:
                res["result"] = res["result"] if res["result"] != "empty" else "red"
                res["detail"] = res["detail"] or "the suite is red and no entry explains it; nothing moved"
                self._tell(res, live)
                return res
            flaky = res.get("flaky")
            flaky_ids = list(flaky) if isinstance(flaky, list) and flaky else []
            flaky_rerun = bool(flaky_ids) and git(self.tree, "rev-parse", "HEAD^{tree}") == first_tree
            self.note.clear()
            if flaky_rerun:
                # SAME TREE FLAKE means the ids are what failed and every other
                # test passed on exactly this tree; another full run costs a whole
                # suite for the same verdict. A rebuilt tree differs, so it gets
                # the full run.
                green, bad, tail = self._guarded_suite(flaky_ids)
            else:
                green, bad, tail = self._guarded_suite(None)
            res["suite_tail"] = tail
            if green and flaky_rerun:
                res["flaky_rerun"] = {"ids": flaky_ids, "tail": tail}
                res["suite_tail"] = first_tail
                self.note.clear()
                self.note.update(first_note)
                self.note["flaky_rerun"] = len(flaky_ids)
            if self.note:
                res[self.note_key] = dict(self.note)
            if not green:
                detail = ("red again when the failing tests ran once more on the batch, though no entry is red alone"
                          if flaky_rerun else
                          "red again on a second full run, though no entry is red alone"
                          if "flaky" in res else "still red after ejecting")
                res.update(result="red", detail=detail + "; nothing moved")
                if "flaky" in res:
                    self._alert("red", "Merge queue: " + res["detail"])
                self._tell(res, live)
                return res
        # Green on this exact head. The base branch moves only now, and only if
        # every gate below passes; any doubt (or exception: run() catches it)
        # leaves it, origin and the service exactly as they were.
        tested = res["tested"] = git(self.tree, "rev-parse", "HEAD")
        why = self._may_fast_forward(base, tested, todo)
        if why:
            res.update(result="waiting", detail=why + "; entries stay queued", merged=[])
            since = self.store.waiting_since(why, self.now())
            mins = int((self.now() - since) / 60)
            self._alert("waiting", f"Merge queue is waiting ({mins} min so far): {why}. {len(todo)} branch(es) queued, tested green.")
            self._tell(res, live)
            return res
        self.store.waiting_since(None, self.now())
        landed = self._land_prs(todo, res) if self.land == "pr" else self._land_local(base, tested, todo, res)
        if not landed:
            self._tell(res, live)
            return res
        self.settle()                       # an earlier ejected entry this batch carried in another branch
        res["result"] = "green"
        try:
            rr = self.restart()
            if rr is None:
                res["restarted"] = None
            else:
                res["restarted"] = getattr(rr, "returncode", 0) == 0
                if not res["restarted"]:
                    res["restart_error"] = (getattr(rr, "stderr", "") or "restart failed").strip()[:300]
        except Exception as exc:
            res["restarted"], res["restart_error"] = False, repr(exc)[:300]
        if self.land == "pr":
            restart_after = ("restarted" if res["restarted"] is True else
                             "restart failed" if res["restarted"] is False else "")
            res["after"] = "; ".join(x for x in (res.get("after", ""), restart_after) if x)
        try:
            self._clean_worktrees(landed)
        except Exception:
            pass
        self._tell(res, live)
        return res

    def _moved(self, e: dict) -> str:
        """Why a queued entry can no longer be what it was queued as, or ''.
        Through a PR the pinned sha is what is pushed, and --match-head-commit
        protects it, so later commits on the branch wait for a later queueing;
        landing locally, a branch that moved on is stale and is queued again."""
        if self.land == "pr" or e.get("pr") is not None:
            return "" if _ok(self.repo, "cat-file", "-e", e["sha"] + "^{commit}") else "the pinned commit is gone"
        head = git(self.repo, "rev-parse", "--verify", "-q", e["branch"], check=False)
        if head == e["sha"]:
            return ""
        return "the branch moved after it was queued (queue it again)" if head else "the branch no longer exists"

    def _land_local(self, base: str, tested: str, todo: list[dict], res: dict) -> list[dict]:
        git(self.repo, "merge", "--ff-only", tested)
        if git(self.repo, "rev-parse", self.base_branch) != tested:
            raise GitError(f"{self.base_branch} is not at the tested commit after the fast-forward")
        res["master_moved"] = True
        res["commits_added"] = int(git(self.repo, "rev-list", "--count", f"{base}..{tested}") or 0)
        passed = (parse_summary(res.get("suite_tail", "")) or {}).get("passed", 0)
        for e in todo:
            self._mark(e, MERGED, _passed_text(passed, res))
            res["merged"].append(e["id"])
        # The base branch has moved. A failing push is reported, never a reason to pretend it did not.
        res["pushed_commits"] = len(git(self.repo, "rev-list", f"origin/{self.base_branch}..{self.base_branch}",
                                        check=False).split())            # counted BEFORE the push
        try:
            pr = self._push()
            res["pushed"] = getattr(pr, "returncode", 1) == 0
            if not res["pushed"]:
                res["push_error"] = (getattr(pr, "stderr", "") or "push failed").strip()[:300]
        except Exception as exc:
            res["pushed"], res["push_error"] = False, repr(exc)[:300]
        return todo

    def _land_prs(self, todo: list[dict], res: dict) -> list[dict]:
        """Each entry in order through its PR, at the tested sha, onto exactly
        the base that was tested. A problem stops the batch there: that entry is
        push-failed, and the rest stay queued, since they were tested together
        with it. The base moving outside the queue stops it too, with nothing
        marked: the next batch tests them again on the new base."""
        passed = (parse_summary(res.get("suite_tail", "")) or {}).get("passed", 0)
        landed, at = [], res["base"]           # where the base must be for the next entry to land
        for i, e in enumerate(todo):
            rest = todo[i + 1:]
            try:
                problem, merged_at = self._land_pr(e, passed, at)
            except _BaseMoved as moved:
                res["waiting"] = [w["branch"] for w in todo[i:]]
                res["detail"] = f"{moved}; {len(todo) - i} branch(es) stay queued, to be tested on the new one"
                break
            if problem:
                self._mark(e, PUSH_FAILED, problem)
                res["push_failed"] = [e["id"]]
                res["waiting"] = [w["branch"] for w in rest]
                res["detail"] = f"{e['branch']} did not land: {problem}" + (
                    f"; {len(rest)} branch(es) stay queued, since they were tested with it" if rest else "")
                break
            if merged_at == at:
                self._mark(e, MERGED, "carried in by an earlier entry's merge")
                res["merged"].append(e["id"])
                landed.append(e)
                _note_for_partner(e, self.partner)
                continue
            res["master_moved"] = True         # first: a crash from here on must still say so
            # GitHub pins a merge's head, never its base or its branch: where it went is checked after the fact.
            parent, on_base = "", False
            if merged_at:
                self._base_now()
                on_base = _ok(self.repo, "merge-base", "--is-ancestor", merged_at, f"origin/{self.base_branch}")
                parent = git(self.repo, "rev-parse", "-q", "--verify", f"{merged_at}^1", check=False)
            wrong = ("" if on_base and parent == at else
                     "GitHub did not say which commit merged it" if not merged_at else
                     f"its merge {merged_at[:12]} is not on {self.base_branch}: the PR went into another branch"
                     if not on_base else
                     f"it merged onto {self.base_branch} at {parent[:12]}, not the tested {at[:12]}: "
                     f"{self.base_branch} moved outside the queue after its last look, so that combination "
                     "was never tested")
            reason = _passed_text(passed, res)
            if wrong:
                reason += f", but {wrong}" if reason else f"but {wrong}"
            self._mark(e, MERGED, reason)
            res["merged"].append(e["id"])
            landed.append(e)
            _note_for_partner(e, self.partner)
            if wrong:
                res["untested"] = [e["id"]]
                res["waiting"] = [w["branch"] for w in rest]
                res["detail"] = f"{e['branch']} merged, but {wrong}" + (
                    f"; {len(rest)} branch(es) stay queued" if rest else "")
                self._alert("untested", "Merge queue: " + res["detail"], every_s=0)
                break
            at = merged_at
        res["pushed"] = False if "push_failed" in res else (True if landed else None)
        if not landed:
            res["result"] = "push_failed" if "push_failed" in res else "waiting"
            return landed
        git(self.repo, "fetch", "-q", "origin", check=False)
        res["after"] = _fast_forward(self.repo, self.base_branch)
        return landed

    def _base_now(self) -> str:
        """The base branch on origin this moment (fetched)."""
        git(self.repo, "fetch", "-q", "origin", self.base_branch)
        return git(self.repo, "rev-parse", f"origin/{self.base_branch}")

    def _check_base(self, at: str) -> None:
        now = self._base_now()
        if now != at:
            raise _BaseMoved(f"{self.base_branch} moved outside the queue (it is at {now[:12]}, "
                             f"the queue expected {at[:12]})")

    def _land_pr(self, e: dict, passed: int, at: str) -> tuple[str, str]:
        """One entry through its PR: ('', the merge commit) once GitHub merged
        it, ('', at) when an earlier entry's merge carried it in, else (the
        problem, ''). Raises _BaseMoved when the base is not `at`, looked at
        first and again right before the merge, since the checks can take half
        an hour."""
        self._check_base(at)
        if _ok(self.repo, "merge-base", "--is-ancestor", e["sha"], at):
            return "", at
        b = e["branch"]
        if e.get("pr") is not None:
            number, problem = e["pr"], _pr_target(self.repo, self.gh, e["pr"], self.base_branch)
        else:
            p = _run(["git", "-C", str(self.repo), "push", "origin", f"{e['sha']}:refs/heads/{b}"])
            if p.returncode:
                return _last_line(p.stderr) or "git push failed", ""
            number, problem = _open_pr(self.repo, self.gh, e, passed, self.base_branch)
        if problem:
            return problem, ""
        waited = _await_checks(self.repo, self.gh, number)
        if waited:
            return waited, ""
        problem = _pr_target(self.repo, self.gh, number, self.base_branch)     # retargeted during the wait?
        if problem:
            return problem, ""
        self._check_base(at)
        problem, merged_at = _merge_pr(self.repo, self.gh, number, e["sha"])
        if not problem and e.get("pr") is None:
            # Only at the sha the queue pushed: work pushed to the branch since stays.
            d = _run(["git", "-C", str(self.repo), "push", f"--force-with-lease=refs/heads/{b}:{e['sha']}",
                      "origin", "--delete", b])
            if d.returncode:
                self.store.log(f"{b}: remote branch not deleted: {_last_line(d.stderr)}")
        return problem, merged_at

    def _build(self, base: str, entries: list[dict], res: dict) -> list[dict]:
        """A fresh integration tree at base with the entries merged in order; the ones that went in."""
        self._fresh_tree(base)
        todo = []
        for e in entries:
            files = self._merge(e)
            if files is None:
                todo.append(e)
            else:
                self._mark(e, CONFLICT, f"conflicts with {self.base_branch} or an earlier entry", files=files)
                res["conflict"].append(e["id"])
        return todo

    def _guarded_suite(self, ids):
        if self.now() > self._deadline:
            raise _Stalled()
        return self._suite(ids)

    def _suite(self, ids):
        """Every suite run in a batch: the first one holds the machine's other work, run() lets it go."""
        if not self._held:
            self._held = True
            try:
                self._hold(True)
            except Exception:
                pass
        return self.suite(self.tree, ids)

    def _may_fast_forward(self, base: str, tested: str, todo: list[dict]) -> Optional[str]:
        """Why the base branch must not move now, or None. Checked as late as possible."""
        if git(self.tree, "status", "--porcelain", "--untracked-files=no", check=False):
            return "the integration tree is not clean"
        for e in todo:       # what was tested is what each entry pinned
            if self._moved(e):
                return f"{e['branch']} moved while the suite ran"
        if self.land == "pr":
            git(self.repo, "fetch", "-q", "origin")
        else:
            why = self._master_ready()
            if why:
                return why
        if git(self.repo, "rev-parse", self._base_ref()) != base:
            return f"{self.base_branch} moved during the batch"
        if git(self.tree, "rev-parse", "HEAD") != tested:     # last, after every other check
            return "the integration tree changed after it was tested"
        return None

    def _find_culprits(self, base, todo, bad, res):
        """The full suite is red on base + all entries. Test the entries one at a
        time, each on the base alone, and eject the red ones; the caller then runs
        the failed ids on the same head or the full suite on a new one. There is
        NO exemption for failures the base already has: if it is red itself the
        queue waits and says so.

        With failing ids named, an entry is red when those ids fail with it in
        (cheap). With none named (a crash, a hang) the entry's whole suite is
        the test. Returns the entries that stay; res["result"] is set when the
        batch cannot go on (master_red, stalled, red)."""
        ids = list(bad) or None
        try:
            # Is the base itself red? Only one that fails these ids (or, with no ids, the whole suite) is.
            self._fresh_tree(base)
            m_ok, m_bad, _ = self._guarded_suite(ids)
            if (ids and set(m_bad) & set(ids)) or (ids is None and not m_ok):
                named = sorted(set(m_bad) & set(ids)) if ids else []
                res.update(result="master_red", detail=f"{self.base_branch} itself is red ({', '.join(named[:5]) or 'the suite'}); nothing merges until it is fixed")
                self._alert("master_red", f"Merge queue is stopped: {res['detail']}")
                return []
            keep, red = [], []
            for e in todo:
                self._fresh_tree(base)
                if self._merge(e) is not None:
                    self._mark(e, CONFLICT, f"conflicts with {self.base_branch} alone")
                    res["conflict"].append(e["id"])
                    continue
                ok, now_bad, _ = self._guarded_suite(ids)
                hit = sorted(set(now_bad) & set(ids)) if ids else []
                if (ids and hit) or (ids is None and not ok):
                    red.append((e, hit))
                else:
                    keep.append(e)
            for e, hit in red:
                self._mark(e, FAILED, "turns the suite red" if hit else "the suite is red with it alone (no test named)", ids=hit)
                res["failed"].append(e["id"])
        except _Stalled:
            res.update(result="stalled", detail="the time budget ran out before the culprit was found; nothing moved")
            self._alert("stalled", "Merge queue stalled: " + res["detail"])
            return []
        if not red:
            # The base passes these ids and so does every entry alone: a flake, or a clash between entries.
            # Rebuild the batch whole and let the caller rerun the ids when its tree is unchanged; otherwise
            # the full suite runs again. Green there ships it, red again moves nothing.
            res["flaky"] = sorted(ids or [])
            return self._build(base, todo, res)
        if not keep:
            res.update(result="red", detail="every queued entry is red alone; nothing moved")
            return []
        self._build(base, keep, res)          # the new head, to be tested whole by the caller
        return keep

    def settle(self) -> list[str]:
        """Entries whose pinned sha is already on the base branch are marked merged, and their branches
        returned: an entry ejected red can land inside another branch's merge, and its red would otherwise
        stay in the status with nothing wrong; a queued one that got there another way has nothing left
        to land, and a batch of it would restart the service for nothing. So is one whose every change
        is already on the base as the same patch (git cherry): a branch rebuilt under another name lands
        as copies with new hashes, and the pinned sha would never get there."""
        settled = []
        for e in self.store.entries():
            if e["state"] not in (QUEUED, CONFLICT, FAILED, STALE, PUSH_FAILED) or not e.get("sha"):
                continue
            if _ok(self.repo, "merge-base", "--is-ancestor", e["sha"], self._base_ref()):
                self._mark(e, MERGED, f"reached {self.base_branch} outside the queue (was {e['state']})")
                settled.append(e["branch"])
                continue
            cherry = git(self.repo, "cherry", self._base_ref(), e["sha"], check=False).splitlines()
            if cherry and all(line.startswith("-") for line in cherry):
                self._mark(e, MERGED,
                           f"the same changes reached {self.base_branch} under other commits (was {e['state']})")
                settled.append(e["branch"])
        return settled

    def _mark(self, e: dict, state: str, reason: str, **extra) -> None:
        e.update(state=state, reason=reason, at=self.now(), **extra)
        self.store.mark(e)
        res = getattr(self, "_res", None)
        if res is not None and "outcomes" in res:
            res["outcomes"].append({"branch": e["branch"], "sha": e["sha"][:12], "state": state, "detail": reason,
                                    "summary": e.get("summary", ""), "at": e["at"]})

    def _clean_worktrees(self, merged: list[dict]) -> None:
        """Remove a merged branch's worktree when its tree is clean (git refuses otherwise)."""
        paths = {}
        cur = None
        for line in git(self.repo, "worktree", "list", "--porcelain", check=False).splitlines():
            if line.startswith("worktree "):
                cur = line[9:]
            elif line.startswith("branch refs/heads/") and cur:
                paths[line[18:]] = cur
        for e in merged:
            p = paths.get(e["branch"])
            if p and Path(p) != self.repo and not git(p, "status", "--porcelain", check=False):
                git(self.repo, "worktree", "remove", p, check=False)

    def _tell(self, res: dict, entries: list[dict]) -> None:
        """Conflicts and failures as a handoff to the session that queued the entry;
        a green batch as one notice naming who is in."""
        by_id = {e["id"]: e for e in entries}
        for key, verb in (("conflict", "conflicts"), ("failed", "turns the suite red"),
                          ("push_failed", "did not land")):
            for i in res.get(key, []):
                e = by_id.get(i)
                if not e:
                    continue
                files = e.get("files") or e.get("ids") or []
                self.handoff(e,
                             f"Merge queue: {e['branch']} ({e['sha'][:8]}) {verb}: {', '.join(files[:8]) or e.get('reason', '')}. "
                             f"Rebase on the new {self.base_branch}, rerun your targeted tests, then queue {e['branch']} again.")
        if res["result"] == "red":
            self._alert("red", f"Merge queue: the suite is red and nothing merged. {res['detail']}")
        if res["result"] == "green" and res.get("restarted") is False:
            self.notify("Merge queue", f"{self.base_branch} moved but the service restart FAILED: {res.get('restart_error', '')}")
        if res["result"] == "green":
            who = sorted({by_id[i].get('speaker') or by_id[i]['branch'] for i in res["merged"] if i in by_id})
            self.notify("Merge queue", f"{len(res['merged'])} merged to {self.base_branch}: {', '.join(who)}."
                        + ("" if res.get("pushed") else " The push FAILED.")
                        + (f" {len(res['failed'])} ejected." if res["failed"] else ""))


# --- for a dashboard -------------------------------------------------------------

def status(store: Optional[Store] = None, next_run: Optional[Callable] = None) -> dict:
    s = store or Store()
    es = s.entries()
    queued = [e for e in es if e["state"] == QUEUED]
    trouble = [{k: e.get(k) for k in ("branch", "speaker", "state", "reason", "files", "ids")}
               for e in es if e["state"] in (CONFLICT, FAILED, STALE, PUSH_FAILED)]
    last = s.last()
    return {"queued": len(queued), "branches": [e["branch"] for e in queued], "trouble": trouble,
            "last": ({k: last.get(k) for k in ("at", "result", "detail", "pushed")}
                     | {"merged": len(last.get("merged", [])), "failed": len(last.get("failed", [])),
                        "conflict": len(last.get("conflict", []))}) if last else None,
            "next_run": next_run() if next_run else None}


# --- one batch from the command line ----------------------------------------------

def run(repo: Path | None = None, base: Path | None = None, suite: str = SUITE, if_queued: bool = False,
        gh: str = "gh", notify=_default_notify, say=print, land: str | None = None) -> int:
    """One batch, as the CLI and the timer run it. Returns 0 for nothing wrong,
    1 when an entry was ejected or did not land (or the suite was red), 2 when
    it could not run. `base` is the state directory; `notify` files a
    "for-<label>:" summary's note once its branch lands."""
    store = Store(base)
    if if_queued and not store.queue():
        return 0
    mode = land or land_mode()
    if mode == "pr":
        why = gh_ready(gh)
        if why:
            say(why)
            return 2
    try:
        checkout = Path(repo) if repo else main_checkout()
    except GitError as ex:
        say(str(ex))
        return 2
    m = Merger(repo=checkout, store=store, tree=store.dir / "int",
               suite=lambda tree, ids: run_command(tree, ids, suite), land=mode, gh=gh, partner=notify,
               base_branch=base_branch(), file_mode=True, notify=lambda title, body: store.log(f"{title}: {body}"))
    res = m.run()
    for o in res.get("outcomes", []):
        store.log(f"{o['branch']} {o['sha']}: {o['state']} {o['detail']}".strip())
        say(f"{o['branch']}: {o['state']}" + (f" ({o['detail']})" if o["detail"] else ""))
    result = res.get("result")
    if result == "busy":
        say("another merger is running")
        return 0
    if result == "empty" and not res.get("stale"):
        say("queue is empty")
        return 0
    if res.get("detail"):
        say(res["detail"])
    if res.get("after"):
        say(res["after"])
    if result == "failed":
        return 2
    if res.get("pushed") is False and res.get("push_error"):
        say("the push failed: " + res["push_error"])
    trouble = any(res.get(k) for k in ("conflict", "failed", "stale", "push_failed", "untested"))
    return 1 if trouble or res.get("pushed") is False or result in ("red", "master_red", "stalled", "push_failed") else 0


# --- the CLI ----------------------------------------------------------------------

def _queue_pr(store: Store, repo: Path, number: int, summary: str, base: str | None = None) -> tuple[int, str]:
    base = base or base_branch()
    if number <= 0:
        return 1, "PR number must be positive"
    pr, problem = _pr_view(repo, "gh", number,
                           "baseRefName,headRefName,headRefOid,isCrossRepository,state,title")
    if not pr:
        return 1, problem
    state = pr.get("state")
    if state != "OPEN":
        return 1, f"refusing PR #{number}: it is {state or 'not open'}, not open"
    if pr.get("isCrossRepository"):
        return 1, f"refusing PR #{number}: it comes from another repository"
    if pr.get("baseRefName") != base:
        return 1, f"refusing PR #{number}: it targets {pr.get('baseRefName') or 'no branch'}, not {base}"
    branch, sha = pr.get("headRefName"), pr.get("headRefOid")
    if not isinstance(branch, str) or not branch or not isinstance(sha, str) or not sha:
        return 1, f"refusing PR #{number}: GitHub did not return a head branch and commit"
    try:
        # Same-repository PRs can be fetched by their head branch. This also
        # leaves the pinned commit available after the collaborator moves on.
        git(repo, "fetch", "origin", branch)
        git(repo, "cat-file", "-e", sha + "^{commit}")
    except GitError as ex:
        return 1, f"cannot fetch PR #{number} head: {ex}"
    entry = store.add(branch, sha, summary or str(pr.get("title") or ""), pr=number)
    return 0, f"queued PR #{number} ({branch}) at {entry['sha'][:12]} ({len(store.queue())} in the queue)"


def _age(t: float) -> str:
    s = int(time.time() - t)
    return f"{s // 3600}h" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_companion.mergeq")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="queue a finished branch, pinned to its current sha")
    a.add_argument("branch", nargs="?")
    a.add_argument("--pr", type=int, metavar="NUMBER", help="queue an existing same-repository PR")
    a.add_argument("--summary", default="")
    r = sub.add_parser("run", help="merge the queue (the timer runs this)")
    r.add_argument("--if-queued", action="store_true", help="exit quietly when the queue is empty")
    r.add_argument("--suite", default=SUITE)
    sub.add_parser("list", help="what is queued")
    s = sub.add_parser("status", help="queued and recent outcomes")
    s.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)
    store = Store()
    if ns.cmd == "add":
        if ns.pr is not None:
            if ns.branch:
                print("use either a branch or --pr, not both", file=sys.stderr)
                return 1
            code, message = _queue_pr(store, Path("."), ns.pr, ns.summary)
            print(message, file=sys.stderr if code else sys.stdout)
            return code
        try:
            branch = ns.branch or git(".", "rev-parse", "--abbrev-ref", "HEAD")
            if branch in (base_branch(), "HEAD"):
                print(f"refusing to queue {base_branch()} or a detached HEAD", file=sys.stderr)
                return 1
            sha = git(".", "rev-parse", "--verify", branch + "^{commit}")
        except GitError as ex:
            print(ex, file=sys.stderr)
            return 1
        store.add(branch, sha, ns.summary)
        print(f"queued {branch} at {sha[:12]} ({len(store.queue())} in the queue)")
        return 0
    if ns.cmd == "run":
        try:
            return run(suite=ns.suite, if_queued=ns.if_queued)
        except GitError as ex:
            print(ex, file=sys.stderr)
            return 2
    q, last, hist = store.queue(), store.last(), store.history()
    if ns.cmd == "status" and ns.json:
        print(json.dumps({"queued": q, "last": last, "recent": hist}, indent=1))
        return 0
    print(f"{len(q)} queued")
    for e in q:
        print(f"  {e['branch']} {e['sha'][:12]} {_age(e['added'])} ago  {e.get('summary', '')}")
    if ns.cmd == "status":
        print("recent")
        for o in hist[-10:]:
            print(f"  {o['branch']} {o['sha']} {o['state']} {_age(o['at'])} ago  {o.get('detail', '')}")
        if not hist:
            print("  none yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
