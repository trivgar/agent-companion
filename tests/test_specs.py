"""Specs: every checkout's specs and plans, and an agent's own (agent_companion.specs)."""
from __future__ import annotations

import pytest

from agent_companion import specs

KEYS = ("CHECKOUT_DIRS", "CHECKOUT_SKIP", "CHECKOUT_PREFER", "SPEC_FOLDERS", "SPECS_SEEN_FILE", "PROJECTS",
        "DATA_DIR")


def _settings(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    for key in KEYS:
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)


def _spec(root, kind, name, text="# Title\n\nBody."):
    d = root / "docs" / "superpowers" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A repository with a worktree, a stale main checkout that is skipped and
    its live worktree, and an extra folder of notes."""
    work = tmp_path / "work"
    site = work / "site"
    (site / ".git").mkdir(parents=True)
    _spec(site, "specs", "2026-10-01-a-design.md", "# Alpha design\n")
    _spec(site, "plans", "2026-10-02-a.md", "# Alpha plan\n")
    wt = work / "worktrees" / "site-branch"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {site}/.git/worktrees/site-branch\n")
    _spec(wt, "specs", "2026-10-01-a-design.md", "# Alpha design, branch copy\n")
    _spec(wt, "specs", "2026-10-03-b-design.md", "# Beta design\n")
    old = work / "shop"
    (old / ".git").mkdir(parents=True)
    _spec(old, "specs", "2026-01-01-stale.md", "# Stale\n")
    live = work / "worktrees" / "shop-main"
    live.mkdir(parents=True)
    (live / ".git").write_text(f"gitdir: {old}/.git/worktrees/shop-main\n")
    _spec(live, "specs", "2026-01-01-stale.md", "# Live copy\n")
    notes = tmp_path / "coord" / "docs"
    notes.mkdir(parents=True)
    (notes / "2026-10-09-radio-plan.md").write_text("# Radio plan\n")
    _settings(tmp_path, monkeypatch,
              f'checkout_dirs = ["{work}", "{work / "worktrees"}"]\ncheckout_skip = ["shop"]\n'
              f'checkout_prefer = ["shop-main"]\nspec_folders = {{notes = "{notes}"}}\n')
    return work


def test_each_spec_once_with_the_main_checkout_first(tree):
    docs = specs.all_docs(fresh=True)
    ids = [d["id"] for d in docs]
    assert ids.count("site/specs/2026-10-01-a-design.md") == 1
    assert "site-branch/specs/2026-10-01-a-design.md" not in ids
    beta = next(d for d in docs if d["name"] == "2026-10-03-b-design.md")
    assert beta["project"] == "site-branch" and beta["repo"] == "site"
    assert ids[0] == "notes/docs/2026-10-09-radio-plan.md"          # newest first by its date
    assert docs[0]["kind"] == "docs" and docs[0]["repo"] == "notes"


def test_a_skipped_checkout_is_never_read(tree):
    docs = specs.all_docs(fresh=True)
    stale = [d for d in docs if d["name"] == "2026-01-01-stale.md"]
    assert [d["project"] for d in stale] == ["shop-main"] and stale[0]["title"] == "Live copy"
    assert "shop" not in specs.roots()


def test_read_refuses_anything_but_a_plain_md_in_its_folder(tree):
    ok = specs.read_id("site/specs/2026-10-01-a-design.md")
    assert ok["ok"] and ok["text"].startswith("# Alpha") and not ok["cut"]
    for bad in ("site/specs/../plans/2026-10-02-a.md", "site/specs/x.txt",
                "site/other/2026-10-01-a-design.md", "nobody/specs/a.md", "shop/specs/2026-01-01-stale.md", ""):
        assert not specs.read_id(bad)["ok"], bad
    link = tree / "site" / "docs" / "superpowers" / "specs" / "link.md"
    link.symlink_to(tree / "shop" / "docs" / "superpowers" / "specs" / "2026-01-01-stale.md")
    assert not specs.read_id("site/specs/link.md")["ok"]
    assert "site/specs/link.md" not in {d["id"] for d in specs.all_docs(fresh=True)}


def test_a_long_document_is_cut(tree):
    _spec(tree / "site", "specs", "2026-10-04-long.md", "# Long\n" + "x" * specs.MAX_BYTES)
    got = specs.read_id("site/specs/2026-10-04-long.md")
    assert got["ok"] and got["cut"] and len(got["text"]) == specs.MAX_BYTES


def test_an_agents_own_project_from_its_cwd(tree):
    got = specs.for_cwd(str(tree / "site" / "app"))
    assert got["project"] == "site"             # the whole repository, its worktree's spec too
    assert {d["id"] for d in got["docs"]} == {"site/specs/2026-10-01-a-design.md",
                                              "site/plans/2026-10-02-a.md", "site-branch/specs/2026-10-03-b-design.md"}
    here = str(tree / "site")
    assert specs.read_in(here, "specs", "2026-10-03-b-design.md", "site-branch")["ok"]
    assert not specs.read_in(here, "specs", "2026-01-01-stale.md", "shop-main")["ok"]   # another repository
    assert specs.for_cwd("/nowhere")["docs"] == []


def test_a_folder_outside_the_checkouts_shows_its_own_documents(tree, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "tool"
    _spec(elsewhere, "plans", "2026-10-05-tool.md", "# Tool plan\n")
    got = specs.for_cwd(str(elsewhere / "src"))
    assert got["project"] == "tool" and [d["id"] for d in got["docs"]] == ["tool/plans/2026-10-05-tool.md"]


def test_the_walk_up_ends_at_the_outermost_listed_folder(tree):
    _spec(tree, "specs", "2026-10-06-top.md", "# Top\n")
    assert specs.root_for(str(tree / "worktrees" / "plain"))[0] == "work"
    _spec(tree.parent, "specs", "2026-10-07-above.md", "# Above\n")
    assert specs.root_for(str(tree / "plain"))[0] == "work"
    (tree / "docs").rename(tree / "docs-off")
    assert specs.root_for(str(tree / "plain")) is None, "never above the listed folders"


def test_live_agents_count_for_their_repo(tree, tmp_path):
    got = specs.live_by_repo([("Ada", str(tree / "worktrees" / "site-branch")),
                              ("Lin", str(tree / "worktrees" / "shop-main")), ("Kit", "/elsewhere"),
                              ("Max", str(tmp_path / "coord"))])
    assert got == {"site": ["Ada"], "shop-main": ["Lin"], "notes": ["Max"]}


def test_without_listed_folders_the_projects_rows_are_read(tmp_path, monkeypatch):
    api = tmp_path / "api"
    _spec(api, "specs", "2026-10-01-api.md", "# API\n")
    _settings(tmp_path, monkeypatch, f'[projects]\n"Our API" = "{api}"\n')
    docs = specs.all_docs(fresh=True)
    assert [d["id"] for d in docs] == ["Our API/specs/2026-10-01-api.md"]
    assert specs.read_id(docs[0]["id"])["ok"] and specs.root_for(str(api / "x"))[0] == "Our API"


def test_a_changed_setting_is_a_new_scan(tree, tmp_path, monkeypatch):
    assert specs.all_docs()
    _settings(tmp_path, monkeypatch, "")
    assert specs.all_docs() == []


def test_the_seen_file_is_a_setting(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, f'data_dir = "{tmp_path / "data"}"\n')
    assert specs.seen_file() == tmp_path / "data" / "specs-seen.json"
    _settings(tmp_path, monkeypatch, f'specs_seen_file = "{tmp_path / "seen.json"}"\n')
    assert specs.seen_file() == tmp_path / "seen.json" and specs.Watcher(None, None).state == tmp_path / "seen.json"


def test_extra_folders_may_come_from_the_environment(tmp_path, monkeypatch):
    _settings(tmp_path, monkeypatch, "")
    monkeypatch.setenv("AGENT_COMPANION_SPEC_FOLDERS", f"a={tmp_path / 'a'}")
    assert specs.folders() == {"a": tmp_path / "a"}
    monkeypatch.setenv("AGENT_COMPANION_SPEC_FOLDERS", "")
    assert specs.folders() == {}


def test_first_sight_is_silent_then_specs_push_and_plans_do_not():
    docs = [{"id": "p/specs/a.md", "kind": "specs", "project": "p", "repo": "p", "title": "A"}]
    new, seen = specs.news(docs, None)
    assert new == [] and seen == {"p/specs/a.md"}
    docs += [{"id": "p/plans/b.md", "kind": "plans", "project": "p", "repo": "p", "title": "B"},
             {"id": "p/specs/c.md", "kind": "specs", "project": "p", "repo": "p", "title": "C"}]
    new, seen = specs.news(docs, seen)
    assert [d["id"] for d in new] == ["p/specs/c.md"] and "p/plans/b.md" in seen


def test_the_push_names_the_one_agent_and_summarises_a_burst():
    one = [{"id": "p/specs/c.md", "kind": "specs", "project": "p", "repo": "p", "title": "C design"}]
    assert specs.message(one, {"p": ["Ada"]}) == ("New spec: C design", "Ada wrote it, in p.", "p/specs/c.md")
    assert specs.message(one, {"p": ["Ada", "Lin"]})[1] == "In p."
    many = [dict(one[0], id=f"p/specs/{i}.md") for i in range(specs.BURST + 1)]
    title, _, doc = specs.message(many, {})
    assert title == f"{specs.BURST + 1} new specs" and doc == ""


def test_an_undelivered_push_is_tried_again(tree, tmp_path):
    state = tmp_path / "seen.json"
    sent = []
    w = specs.Watcher(lambda *a: sent.append(a) or False, lambda: {}, state)
    w.tick()                                           # first sight: silent, recorded
    assert not sent and state.exists()
    _spec(tree / "site", "specs", "2026-10-09-new-design.md", "# New one\n")
    w.tick()
    assert len(sent) == 1 and "New one" in sent[0][0] and "doc=site/specs/2026-10-09-new-design.md" in sent[0][2]
    w.tick()                                           # not delivered, so not remembered
    assert len(sent) == 2
    w.send = lambda *a: sent.append(a) or True
    w.tick(); w.tick()
    assert len(sent) == 3


def test_the_watcher_never_starts_under_tests(tree, tmp_path):
    specs.Watcher(lambda *a: pytest.fail("sent"), lambda: {}, tmp_path / "s.json").start()
    assert not (tmp_path / "s.json").exists()
