"""The checkouts Specs and Files read: listed folders' children, else [projects]."""
from __future__ import annotations

import os

from agent_companion import checkouts

KEYS = ("CHECKOUT_DIRS", "CHECKOUT_SKIP", "CHECKOUT_PREFER", "PROJECTS")


def _settings(tmp_path, monkeypatch, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    for key in KEYS:
        monkeypatch.delenv("AGENT_COMPANION_" + key, raising=False)


def _work(tmp_path):
    """site (main) and its worktree site-branch; shop (a stale main checkout)
    and its live worktree shop-main; a plain folder; a dot folder."""
    work = tmp_path / "work"
    site, shop = work / "site", work / "shop"
    (site / ".git").mkdir(parents=True)
    (shop / ".git").mkdir(parents=True)
    for name, main in (("site-branch", site), ("shop-main", shop)):
        wt = work / "worktrees" / name
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/{name}\n")
    (work / "notes").mkdir()
    (work / ".cache").mkdir()
    return work


def test_the_children_of_each_listed_folder_are_the_checkouts(tmp_path, monkeypatch):
    work = _work(tmp_path)
    _settings(tmp_path, monkeypatch, f'checkout_dirs = ["{work}", "{work / "worktrees"}"]\n')
    got = dict(checkouts.candidates())
    assert sorted(got) == ["notes", "shop", "shop-main", "site", "site-branch"]
    assert "worktrees" not in got and ".cache" not in got, "a listed folder is never itself a checkout"


def test_a_repositorys_checkouts_group_with_the_main_first(tmp_path, monkeypatch):
    work = _work(tmp_path)
    _settings(tmp_path, monkeypatch, f'checkout_dirs = ["{work}", "{work / "worktrees"}"]\n')
    groups = checkouts.grouped(lambda d: (d / ".git").exists())
    assert sorted([n for n, _ in g] for g in groups) == [["shop", "shop-main"], ["site", "site-branch"]]


def test_a_skipped_main_checkout_gives_the_head_to_the_preferred(tmp_path, monkeypatch):
    work = _work(tmp_path)
    (work / "worktrees" / "shop-old").mkdir()
    (work / "worktrees" / "shop-old" / ".git").write_text(f"gitdir: {work / 'shop'}/.git/worktrees/shop-old\n")
    _settings(tmp_path, monkeypatch, f'checkout_dirs = ["{work}", "{work / "worktrees"}"]\n'
              'checkout_skip = ["shop"]\ncheckout_prefer = ["shop-main"]\n')
    groups = {g[0][0]: [n for n, _ in g] for g in checkouts.grouped(lambda d: (d / ".git").exists())}
    assert groups["shop-main"] == ["shop-main", "shop-old"] and "shop" not in sum(groups.values(), [])


def test_without_listed_folders_the_projects_rows_are_the_checkouts(tmp_path, monkeypatch):
    work = _work(tmp_path)
    _settings(tmp_path, monkeypatch, f'[projects]\nSite = "{work / "site"}"\nbranch = "{work / "worktrees" / "site-branch"}"\n'
              f'gone = "{tmp_path / "nowhere"}"\n')
    assert [n for n, _ in checkouts.candidates()] == ["Site", "branch"]
    assert [[n for n, _ in g] for g in checkouts.grouped(lambda d: True)] == [["Site", "branch"]]


def test_a_worktree_belongs_to_its_main_checkouts_repository(tmp_path):
    work = _work(tmp_path)
    assert checkouts.repo_of(work / "worktrees" / "site-branch") == str(work / "site" / ".git")
    assert checkouts.repo_of(work / "site") == str(work / "site" / ".git")
    assert checkouts.repo_of(work / "notes") == str(work / "notes")


def test_the_lists_may_come_from_the_environment(tmp_path, monkeypatch):
    work = _work(tmp_path)
    _settings(tmp_path, monkeypatch, "")
    monkeypatch.setenv("AGENT_COMPANION_CHECKOUT_DIRS", os.pathsep.join([str(work), str(work / "worktrees")]))
    monkeypatch.setenv("AGENT_COMPANION_CHECKOUT_SKIP", "shop" + os.pathsep + "notes")
    assert sorted(n for n, _ in checkouts.candidates()) == ["notes", "shop", "shop-main", "site", "site-branch"]
    assert checkouts.skip() == {"shop", "notes"}
    assert checkouts.key() != (checkouts.dirs(), frozenset(), checkouts.prefer(), ())
