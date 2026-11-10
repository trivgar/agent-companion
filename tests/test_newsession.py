"""New session: a configured project's key starts Claude, never a path.

Nothing here starts tmux or claude: every launch gets a fake runner."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest

from agent_companion import newsession as ns

CP = subprocess.CompletedProcess


def _settings(tmp_path: Path, monkeypatch, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    for key in ("PROJECTS", "NEW_SESSION"):
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)


# ---- the projects and the pure helpers


def test_a_projects_row_may_name_its_key_label_and_tmux_prefix(tmp_path, monkeypatch):
    site, api, docs = tmp_path / "site", tmp_path / "api", tmp_path / "docs"
    _settings(tmp_path, monkeypatch,
              f'[[projects]]\nname = "Web site"\npath = "{site}"\nkey = "site"\nprefix = "claude-site"\n\n'
              f'[[projects]]\nname = "api"\npath = "{api}"\n\n'
              f'[[projects]]\nname = "docs"\npath = "{docs}"\nlabel = "Handbook"\nprefix = "docs.v2:main"\n\n'
              f'[[projects]]\nname = "again"\npath = "{api}"\nkey = "SITE"\n')
    assert ns.projects() == (
        {"key": "site", "label": "Web site", "cwd": str(site.resolve()), "prefix": "claude-site"},
        {"key": "api", "label": "api", "cwd": str(api.resolve()), "prefix": "api"},
        {"key": "docs", "label": "Handbook", "cwd": str(docs.resolve()), "prefix": "docs-v2-main"},
    ), "a key is taken once, and a prefix is made tmux-safe"


def test_a_plain_projects_table_gives_each_name_as_key_and_label(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, f'[projects]\n"My app" = "{tmp_path}"\n')
    assert ns.projects() == ({"key": "My app", "label": "My app", "cwd": str(tmp_path.resolve()),
                              "prefix": "My-app"},)


def test_the_new_session_settings_add_claude_args_and_a_model(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch,
              '[new_session]\nclaude_args = ["--permission-mode", "acceptEdits", "", 3]\ndefault_model = " sonnet "\n')
    assert ns.claude_args() == ["--permission-mode", "acceptEdits"]
    assert ns.default_model() == "sonnet"
    _settings(tmp_path, monkeypatch, '[new_session]\nclaude_args = "--verbose"\n')
    assert ns.claude_args() == [] and ns.default_model() == ""


def _configured(tmp_path):
    (tmp_path / "site").mkdir(exist_ok=True)
    return ({"key": "site", "label": "Site", "cwd": str(tmp_path / "site"), "prefix": "claude-site"},
            {"key": "api", "label": "API", "cwd": "/home/me/work/api", "prefix": "api"},
            {"key": "shop", "label": "Shop", "cwd": "/home/me/work/web-shop", "prefix": "claude-shop"})


def test_a_key_finds_its_project_and_nothing_else_does(tmp_path):
    configured = _configured(tmp_path)
    assert ns.project("api", configured)["cwd"] == "/home/me/work/api"
    for bad in ("", "nope", "/home/me/work/api", "../api", "API", "api ", None, 3, ["api"], {"key": "api"}):
        assert ns.project(bad, configured) is None, bad


def test_the_first_message_is_optional_one_line_and_capped():
    assert ns.clean_message(None) == ("", "")
    assert ns.clean_message("") == ("", "")
    assert ns.clean_message("  fix\nthe   build ") == ("fix the build", "")
    assert ns.clean_message("x" * 4000) == ("x" * 4000, "")
    assert ns.clean_message("x" * 4001) == ("", "the first message is over 4,000 characters")
    assert ns.clean_message(42) == ("", "the first message must be text")


def test_control_characters_never_act_on_the_typed_line():
    """The message is typed into a shell, where a ^C would drop the line and
    run what follows as a command of its own, and ^O or ESC would act on it.
    Every control character lands as a space; an emoji's joiners are kept."""
    words, why = ns.clean_message("hi\x03rm -rf ~/work #\x0f\x1b[A\x7fdone\x85")
    assert why == "" and words == "hi rm -rf ~/work # [A done"
    line = ns.command("claude-site", words)
    assert not any(ord(c) < 32 or 127 <= ord(c) < 160 for c in line), line
    assert ns.clean_message("ship it \U0001F468‍\U0001F469‍\U0001F467")[0] == \
        "ship it \U0001F468‍\U0001F469‍\U0001F467"


def test_a_message_that_looks_like_a_flag_is_not_one():
    words, why = ns.clean_message("--model haiku")
    assert why == "" and not words.startswith("-") and words.strip() == "--model haiku"


def test_the_tmux_name_is_the_prefix_then_counts_past_the_taken():
    assert ns.tmux_name("claude-site", set()) == "claude-site"
    assert ns.tmux_name("claude-site", {"claude-site"}) == "claude-site-2"
    assert ns.tmux_name("claude-site", {"claude-site", "claude-site-2", "claude-site-3"}) == "claude-site-4"
    assert ns.tmux_name("api", {"api-1", "api-2"}) == "api"


@pytest.mark.parametrize("msg", ['"; rm -rf ~ #', "$(reboot)", "it's `id` && echo $HOME", ""])
def test_the_command_carries_the_message_as_one_literal_argument(msg):
    argv = shlex.split(ns.command("claude-site", msg, ["--permission-mode", "acceptEdits"], "sonnet"))
    assert argv[:7] == ["claude", "--permission-mode", "acceptEdits", "--model", "sonnet", "--name", "claude-site"]
    assert argv[7:] == ([msg] if msg else [])
    assert shlex.split(ns.command("claude-site", msg)) == ["claude", "--name", "claude-site"] + ([msg] if msg else [])


def test_busy_note_words():
    assert ns.busy_note([], "API") == ""
    assert ns.busy_note(["Ada"], "API") == "Ada is in API"
    assert ns.busy_note(["Ada", "Lin", "Kit"], "API") == "Ada, Lin and Kit are in API"
    assert ns.busy_note(["an unnamed session"], "Site") == "An unnamed session is in Site"


def test_the_listing_says_who_already_works_where_and_never_a_path(tmp_path):
    configured = _configured(tmp_path)
    rows = [
        {"pid": 11, "speaker": "Ada", "cwd": "/home/me/work/api", "child": False},
        {"pid": 12, "speaker": "", "cwd": "/home/me/work/api/src", "child": False},
        {"pid": 13, "speaker": "Helper", "cwd": str(tmp_path / "site"), "child": True},
        {"pid": 14, "speaker": "Lin", "cwd": "/home/me/work/web", "child": False},
        {"pid": 15, "speaker": "Kit", "cwd": "/home/me/work/web-shop", "child": False},
    ]
    out = ns.listing(rows, configured)
    assert [p["key"] for p in out] == ["site", "api", "shop"]
    got = {p["key"]: p for p in out}
    assert got["api"]["sessions"] == [{"speaker": "Ada", "pid": 11}, {"speaker": "", "pid": 12}]
    assert got["api"]["note"] == "Ada and an unnamed session are in API"
    assert got["shop"]["note"] == "Kit is in Shop", "web is not web-shop"
    assert got["site"] == {"key": "site", "label": "Site", "sessions": [], "note": "", "available": True,
                           "reason": ""}, "a helper is not a session"
    assert (got["api"]["available"], got["api"]["reason"]) == (False, "Folder does not exist")
    assert "/home/" not in json.dumps(out) and str(tmp_path) not in json.dumps(out)
    assert ns.listing(None, configured)[0]["sessions"] == []


def _proc(root, pid, comm, cwd, tty):
    d = root / str(pid)
    (d / "fd").mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    os.symlink(cwd, d / "cwd")
    os.symlink(tty, d / "fd" / "0")


def test_find_claude_wants_claude_in_that_folder_on_that_pane(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    want = str(proj.resolve())
    root = tmp_path / "proc"
    _proc(root, 100, "bash", want, "/dev/pts/7")
    _proc(root, 101, "claude", want, "/dev/pts/9")
    _proc(root, 102, "claude", str(tmp_path.resolve()), "/dev/pts/7")
    (root / "self").mkdir()
    assert ns.find_claude(want, "/dev/pts/7", proc=root) is None
    _proc(root, 103, "claude", want, "/dev/pts/7")
    assert ns.find_claude(want, "/dev/pts/7", proc=root) == 103
    assert ns.find_claude(want, "", proc=root) is None
    assert ns.find_claude(want, "/dev/pts/7", proc=tmp_path / "no-proc") is None


def test_an_npm_install_of_claude_is_found_as_node(tmp_path):
    want = str(tmp_path.resolve())
    root = tmp_path / "proc"
    _proc(root, 200, "node", want, "/dev/pts/3")
    assert ns.find_claude(want, "/dev/pts/3", proc=root) == 200


# ---- launch, with a fake tmux


class FakeTmux:
    """Answers the tmux calls launch() makes and records every argv."""

    def __init__(self, sessions=(), pane="me@box:~$ claude --name claude-site\n"
                                          "bash: claude: command not found\n\n"):
        self.calls, self.sessions, self.pane = [], set(sessions), pane

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if "list-sessions" in argv:
            return CP(argv, 0, "".join(s + "\n" for s in sorted(self.sessions)), "")
        if "new-session" in argv:
            self.sessions.add(argv[argv.index("-s") + 1])
        if "display-message" in argv:
            return CP(argv, 0, "/dev/pts/7\n", "")
        if "capture-pane" in argv:
            return CP(argv, 0, self.pane, "")
        return CP(argv, 0, "", "")

    def call(self, word):
        return [c for c in self.calls if word in c]


@pytest.fixture
def clock(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(ns, "SLEEP", lambda s: t.__setitem__(0, t[0] + s))
    monkeypatch.setattr(ns, "CLOCK", lambda: t[0])
    return t


@pytest.fixture
def tools(monkeypatch):
    """Which programs are installed: tmux and systemd-run unless a test drops one."""
    have = {"tmux", "systemd-run"}
    monkeypatch.setattr(ns.shutil, "which", lambda n: f"/usr/bin/{n}" if n in have else None)
    return have


def _proj(tmp_path):
    return {"key": "site", "label": "Site", "cwd": str(tmp_path), "prefix": "claude-site"}


def test_a_launch_types_the_command_literally_and_sees_claude_start(tmp_path, monkeypatch, clock, tools):
    _settings(tmp_path, monkeypatch, '[new_session]\nclaude_args = ["--permission-mode", "acceptEdits"]\n')
    run = FakeTmux(sessions={"claude-site"})
    answers = iter([None, None, 4242])
    looked = []

    def find(cwd, tty):
        looked.append((cwd, tty))
        return next(answers)
    msg = '"; rm -rf ~ # $(reboot)'
    r = ns.launch(_proj(tmp_path), msg, run=run, find=find)
    assert r == {"ok": True, "status": 200, "tmux": "claude-site-2", "pid": 4242, "detail": "", "log": "started"}
    new = run.call("new-session")[0]
    assert new[new.index("new-session"):] == ["new-session", "-d", "-s", "claude-site-2", "-c", str(tmp_path)]
    typed = [c for c in run.call("send-keys") if "-l" in c]
    assert len(typed) == 1
    c = typed[0]
    assert c[c.index("-t") + 1] == "=claude-site-2:" and c[-2] == "--"
    assert shlex.split(c[-1]) == ["claude", "--permission-mode", "acceptEdits", "--name", "claude-site-2", msg]
    enter = [x for x in run.call("send-keys") if x[-1] == "Enter"]
    assert len(enter) == 1 and run.calls.index(enter[0]) > run.calls.index(c), "Enter after the line"
    assert looked == [(str(tmp_path), "/dev/pts/7")] * 3


def test_the_tmux_server_is_born_in_its_own_scope(tmp_path, clock, tools):
    run = FakeTmux()
    ns.launch(_proj(tmp_path), "", run=run, find=lambda c, t: 1)
    new = run.call("new-session")
    assert len(new) == 1 and new[0][:5] == ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect"]
    assert any(a.startswith("--unit=agent-companion-newsession-claude-site-") for a in new[0])


def test_no_scope_to_be_had_is_plain_tmux(tmp_path, clock, tools):
    class NoBus(FakeTmux):
        def __call__(self, argv, **kw):
            if str(argv[0]).endswith("systemd-run"):
                self.calls.append([str(a) for a in argv])
                raise subprocess.CalledProcessError(1, argv, "", "Failed to connect to bus")
            return super().__call__(argv, **kw)
    run = NoBus()
    r = ns.launch(_proj(tmp_path), "", run=run, find=lambda c, t: 9)
    assert r["ok"] is True and r["pid"] == 9
    new = run.call("new-session")
    assert len(new) == 2 and new[1][:2] == ["/usr/bin/tmux", "new-session"]
    tools.discard("systemd-run")
    run = FakeTmux()
    assert ns.launch(_proj(tmp_path), "", run=run, find=lambda c, t: 9)["ok"] is True
    assert [c[:2] for c in run.call("new-session")] == [["/usr/bin/tmux", "new-session"]]


def test_no_claude_in_twelve_seconds_is_a_500_in_words_and_the_window_stays(tmp_path, clock, tools):
    run = FakeTmux()
    r = ns.launch(_proj(tmp_path), "secret plan", run=run, find=lambda c, t: None)
    assert (r["ok"], r["status"], r["tmux"], r["pid"]) == (False, 500, "claude-site", None)
    assert r["detail"].startswith("No Claude started in claude-site within 12 seconds.")
    assert "bash: claude: command not found" in r["detail"] and "left open" in r["detail"]
    assert clock[0] >= ns.VERIFY_S
    assert not run.call("kill-session"), "the tmux session is left for a look"
    assert "secret plan" not in r["log"] and "command not found" not in r["log"]


def test_a_blank_window_says_so(tmp_path, clock, tools):
    r = ns.launch(_proj(tmp_path), "", run=FakeTmux(pane="\n\n"), find=lambda c, t: None)
    assert "Its window was blank." in r["detail"]


def test_tmux_refusing_is_a_500_in_words(tmp_path, clock, tools):
    def run(argv, **kw):
        if "list-sessions" in argv:
            return CP(argv, 0, "", "")
        raise subprocess.CalledProcessError(1, argv, "", "duplicate session: claude-site")
    r = ns.launch(_proj(tmp_path), "", run=run, find=lambda c, t: 1)
    assert r["ok"] is False and r["status"] == 500
    assert r["detail"] == "tmux refused to start claude-site: duplicate session: claude-site"


def test_tmux_not_answering_starts_nothing(tmp_path, clock, tools):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return CP(argv, 1, "", "permission denied")
    r = ns.launch(_proj(tmp_path), "", run=run, find=lambda c, t: 1)
    assert r["status"] == 500 and r["detail"] == "tmux did not answer, so nothing was started"
    assert not any("new-session" in c for c in calls)


def test_no_tmux_installed_starts_nothing(tmp_path, clock, tools):
    tools.discard("tmux")
    run = FakeTmux()
    r = ns.launch(_proj(tmp_path), "", run=run, find=lambda c, t: 1)
    assert r["status"] == 500 and r["detail"] == "tmux is not installed, so nothing was started"
    assert not run.calls


def test_a_missing_folder_starts_nothing(tmp_path, clock, tools):
    run = FakeTmux()
    r = ns.launch({**_proj(tmp_path), "cwd": str(tmp_path / "gone")}, "", run=run, find=lambda c, t: 1)
    assert r["status"] == 500 and r["detail"] == "Site's folder is not there, so nothing was started"
    assert not run.calls


def test_nothing_launches_under_pytest_without_a_fake(tmp_path):
    r = ns.launch(_proj(tmp_path), "")
    assert (r["ok"], r["status"]) == (False, 503)


def test_the_log_line_has_the_length_never_the_words():
    e = ns.log_entry("ada", "site", "claude-site", True, "started", 11, now=5.0)
    assert e == {"at": 5.0, "to": "claude-site", "text": "[new session: site]", "ok": True,
                 "detail": "started", "who": "ada", "project": "site", "tmux": "claude-site",
                 "message_len": 11}
    assert ns.log_entry("ada", "unknown", "", False, "x", 0, now=1.0)["to"] == "new session"


def test_the_log_line_reads_back_in_the_chat_thread(tmp_path):
    from agent_companion.server import chat
    f = tmp_path / "chat.jsonl"
    chat.record(ns.log_entry("ada", "site", "claude-site", False, "x", 0, now=5.0), f)
    assert chat.thread({}, tmp_path, path=f)[0]["text"] == "[new session: site]"


def test_launcher_rows_need_a_key_and_a_one_word_program(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch,
              '[new_session]\n'
              '[[new_session.launchers]]\nkey = "grok"\nlabel = "Grok"\nprogram = "~/bin/claude-or"\nmodel = " x-ai/grok-4.7 "\n'
              '[[new_session.launchers]]\nkey = "GROK"\nprogram = "claude"\n'
              '[[new_session.launchers]]\nkey = "evil"\nprogram = "claude; reboot"\n'
              '[[new_session.launchers]]\nkey = "flag"\nprogram = "-x"\n'
              '[[new_session.launchers]]\nprogram = "claude"\n'
              '[[new_session.launchers]]\nkey = "plain"\nprogram = "claude"\n')
    assert ns.launchers() == (
        {"key": "grok", "label": "Grok", "program": str(Path("~/bin/claude-or").expanduser()), "model": "x-ai/grok-4.7"},
        {"key": "plain", "label": "plain", "program": "claude", "model": ""})
    assert ns.launcher("grok")["model"] == "x-ai/grok-4.7"
    assert ns.launcher("Grok") is None and ns.launcher(None) is None
    assert ns.launcher_listing() == [{"key": "grok", "label": "Grok"}, {"key": "plain", "label": "plain"}]


def test_a_launch_with_a_launcher_types_its_program_and_model(tmp_path, monkeypatch, clock, tools):
    _settings(tmp_path, monkeypatch, '[new_session]\ndefault_model = "sonnet"\n')
    run = FakeTmux()
    how = {"key": "grok", "label": "Grok", "program": "/home/me/bin/claude-or", "model": "x-ai/grok-4.7"}
    r = ns.launch(_proj(tmp_path), "hi", run=run, find=lambda cwd, tty: 5, launcher=how)
    assert r["ok"]
    c = [c for c in run.call("send-keys") if "-l" in c][0]
    assert shlex.split(c[-1]) == ["/home/me/bin/claude-or", "--model", "x-ai/grok-4.7", "--name", "claude-site", "hi"]
