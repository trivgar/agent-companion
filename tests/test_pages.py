"""Agent pages: private storage, route authorization, and the sealed response."""
from __future__ import annotations

import stat

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from agent_companion import addons, pages, server  # noqa: E402
from agent_companion.server import auth  # noqa: E402
from agent_companion.server import pages as page_routes  # noqa: E402


TOKEN = "t" * 32
OWNER_KEY = "o" * 64
OWNER = {auth.OWNER_HEADER: OWNER_KEY}
SAME = {"origin": "http://testserver", "host": "testserver"}


def _publish(store, folder, source, **more):
    data = {
        "html": "<h1>Hello</h1>", "title": "Hello", "folder": folder, "source": source,
        "speaker": "agent", "project": "project", "household": False,
    }
    data.update(more)
    return store.publish(**data)


def test_private_directory_replacement_visibility_and_modes(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    clock = iter((100.0, 200.0))
    store = pages.Store(tmp_path / "pages", now=lambda: next(clock), private=[private])
    pushed = []

    with pytest.raises(pages.Refused, match="pages_private_dirs"):
        _publish(store, private, private / "page.html", household=True)

    first, made = _publish(store, tmp_path, tmp_path / "one.html", household=True,
                           pusher=lambda *args: pushed.append(args))
    second, remade = _publish(store, tmp_path, tmp_path / "one.html", html="<p>new</p>",
                              title="New", household=True, pusher=lambda *args: pushed.append(args))
    assert made and not remade
    assert second["id"] == first["id"] and second["created"] == first["created"]
    assert second["updated"] == 200.0 and store.html(first["id"]) == "<p>new</p>"
    assert pushed == [("owner", "Page from agent (project)", "Hello", f"/?view=pages&page={first['id']}", "pages")]
    assert pages.can_see(first, "owner") and pages.can_see(first, "shared")
    assert not pages.can_see({**first, "household": False}, "shared")
    assert all("source" not in page for page in store.list_for("owner"))

    folder = tmp_path / "pages" / first["id"]
    assert stat.S_IMODE((tmp_path / "pages").stat().st_mode) == 0o700
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "page.html").stat().st_mode) == 0o600
    assert stat.S_IMODE((folder / "meta.json").stat().st_mode) == 0o600


def test_size_limit_is_counted_in_utf8_bytes(tmp_path):
    store = pages.Store(tmp_path / "pages")
    with pytest.raises(pages.Refused, match="limit"):
        _publish(store, tmp_path, tmp_path / "too-large.html", html="x" * (pages.MAX_BYTES + 1))


def test_owner_for_controls_owner_pusher_and_replacement(tmp_path):
    folders = []

    def owner_for(folder):
        folders.append(folder)
        return "partner"

    store = pages.Store(tmp_path / "pages", owner_for=owner_for)
    pushed = []
    first, made = _publish(store, tmp_path, tmp_path / "page.html",
                           pusher=lambda *args: pushed.append(args))
    second, remade = _publish(store, tmp_path, tmp_path / "page.html", title="Updated",
                              pusher=lambda *args: pushed.append(args))

    assert made and not remade
    assert first["owner"] == second["owner"] == "partner"
    assert first["id"] == second["id"]
    assert pushed == [("partner", "Page from agent (project)", "Hello",
                       f"/?view=pages&page={first['id']}", "pages")]
    assert folders == [tmp_path.resolve(), tmp_path.resolve()]


def test_household_refusal_hook_replaces_private_directory_check(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    calls = []

    def refusal(folder, source, owner):
        calls.append((folder, source, owner))
        return "no"

    refusing = pages.Store(tmp_path / "refusing", private=[private], household_refusal=refusal)
    with pytest.raises(pages.Refused, match="^no$"):
        _publish(refusing, private, private / "page.html", household=True)
    _publish(refusing, tmp_path, tmp_path / "owner-only.html", household=False)
    assert calls == [(private.resolve(), (private / "page.html").resolve(), "owner")]

    allowing = pages.Store(tmp_path / "allowing", private=[private],
                           household_refusal=lambda folder, source, owner: None)
    record, made = _publish(allowing, private, private / "allowed.html", household=True)
    assert made and record["household"] is True


def test_visible_hook_accepts_a_host_viewer_and_default_visibility_remains(tmp_path):
    viewer = object()
    seen = []

    def visible(record, actual_viewer):
        seen.append((record["title"], actual_viewer))
        return record["title"].startswith("A")

    store = pages.Store(tmp_path / "custom", visible=visible)
    _publish(store, tmp_path, tmp_path / "alpha.html", title="Alpha")
    _publish(store, tmp_path, tmp_path / "beta.html", title="Beta")
    assert [record["title"] for record in store.list_for(viewer)] == ["Alpha"]
    assert {title for title, _ in seen} == {"Alpha", "Beta"}
    assert all(actual_viewer is viewer for _, actual_viewer in seen)

    default = pages.Store(tmp_path / "default")
    _publish(default, tmp_path, tmp_path / "default.html", title="Alpha")
    assert [record["title"] for record in default.list_for("owner")] == ["Alpha"]
    assert default.list_for("shared") == []


@pytest.fixture
def client(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv("AGENT_COMPANION_PAGES_DIR", str(tmp_path / "pages"))
    monkeypatch.setenv("AGENT_COMPANION_PAGES_PRIVATE_DIRS", str(private))
    monkeypatch.setattr(server, "FEATURES", ["pages"])
    sent = []
    monkeypatch.setattr(page_routes, "_push", lambda *args: sent.append(args) or 1)
    return TestClient(server.create_app(token=TOKEN, owner_key=OWNER_KEY)), tmp_path, private, sent


def _body(root, **more):
    body = {"html": "<script>window.x = 1</script>", "title": "A page", "folder": str(root),
            "source": str(root / "page.html"), "speaker": "agent", "project": "project"}
    body.update(more)
    return body


def test_routes_use_the_hook_token_and_page_headers_are_exact(client):
    c, root, private, sent = client
    assert c.get("/api/features").json() == [{"id": "pages", "title": "Pages"}]
    assert "CC.register('pages'" in c.get("/static/pages.js").text
    assert c.post("/api/pages", json=_body(root)).status_code == 401
    made = c.post("/api/pages", json=_body(root), headers={auth.HEADER_NAME: TOKEN})
    assert made.status_code == 200 and made.json()["created"] is True
    page_id = made.json()["page"]["id"]
    assert c.get("/api/pages").status_code == 401
    listed = c.get("/api/pages", headers=OWNER).json()["pages"]
    assert listed[0]["id"] == page_id and "source" not in listed[0]

    got = c.get(f"/pages/{page_id}", headers=OWNER)
    assert got.status_code == 200 and got.text == _body(root)["html"]
    assert got.headers["content-security-policy"] == pages.PAGE_HEADERS["Content-Security-Policy"]
    for name in ("x-content-type-options", "referrer-policy", "cache-control", "content-type"):
        expected = next(value for key, value in pages.PAGE_HEADERS.items() if key.lower() == name)
        assert got.headers[name].startswith(expected)
    assert "allow-same-origin" not in got.headers["content-security-policy"]
    assert c.get(f"/pages/{page_id}?k=not-a-key").status_code == 401
    assert sent == [("owner", "Page from agent (project)", "A page", f"/?view=pages&page={page_id}", "pages")]

    replaced = c.post("/api/pages", json=_body(root, title="Changed"), headers={auth.HEADER_NAME: TOKEN})
    assert replaced.status_code == 200 and replaced.json()["created"] is False
    assert replaced.json()["page"]["id"] == page_id and len(sent) == 1
    assert c.post("/api/pages", json=_body(private, folder=str(private), household=True),
                  headers={auth.HEADER_NAME: TOKEN}).status_code == 400


def test_a_shared_person_cannot_see_or_delete_owner_only_pages(client, monkeypatch):
    c, root, _, _ = client
    made = c.post("/api/pages", json=_body(root), headers={auth.HEADER_NAME: TOKEN}).json()
    page_id = made["page"]["id"]
    monkeypatch.setattr(addons, "person", lambda request: {"id": "shared"})

    assert c.get("/api/pages").json() == {"pages": []}
    assert c.get(f"/pages/{page_id}").status_code == 404
    assert c.request("DELETE", f"/api/pages/{page_id}", json={}, headers=SAME).status_code == 404
    assert c.request("DELETE", f"/api/pages/{page_id}", json={}, headers={**OWNER, **SAME}).json() == {"ok": True}
    assert c.get(f"/pages/{page_id}", headers=OWNER).status_code == 404


def test_shared_page_is_available_only_to_an_existing_shared_identity(client, monkeypatch):
    c, root, _, _ = client
    made = c.post("/api/pages", json=_body(root, household=True), headers={auth.HEADER_NAME: TOKEN}).json()
    page_id = made["page"]["id"]
    monkeypatch.setattr(addons, "person", lambda request: {"id": "shared"})
    assert [page["id"] for page in c.get("/api/pages").json()["pages"]] == [page_id]
    assert c.get(f"/pages/{page_id}").status_code == 200
    assert c.request("DELETE", f"/api/pages/{page_id}", json={}, headers=SAME).status_code == 404


def test_can_view_follows_the_visible_hook_and_defaults_to_can_see(tmp_path):
    record = {"id": "p1", "owner": "owner", "household": True}
    assert pages.Store(tmp_path / "pages").can_view(record, "partner")
    strict = pages.Store(tmp_path / "pages", visible=lambda rec, viewer: viewer == rec["owner"])
    assert strict.can_view(record, "owner")
    assert not strict.can_view(record, "partner")


def test_the_app_tag_is_the_names_letters_and_digits_then_app():
    assert pages.app_tag("Hilltop") == "HilltopApp"
    assert pages.app_tag("Hilltop Two") == "HilltopTwoApp"
    assert pages.app_tag("Hill-top!") == "HilltopApp"
    assert pages.app_tag("") == ""


BROWSER = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"


def test_an_app_without_the_seal_is_told_from_a_sealed_one_and_from_a_browser():
    unsealed = BROWSER + " HilltopApp"
    assert pages.unsealed_app(unsealed, "Hilltop")
    assert not pages.unsealed_app(unsealed + " CompanionSealed/1", "Hilltop")
    assert not pages.unsealed_app(BROWSER, "Hilltop")
    assert not pages.unsealed_app(BROWSER + " NotHilltopApp", "Hilltop")
    assert not pages.unsealed_app(None, "Hilltop")
    for name in ("", None):
        assert not pages.unsealed_app(unsealed, name)


def test_an_unsealed_app_gets_the_notice_and_never_the_page(client, monkeypatch):
    c, root, _, _ = client
    marker = "marker-7f3a9c"
    made = c.post("/api/pages", json=_body(root, html=f"<p>{marker}</p>"), headers={auth.HEADER_NAME: TOKEN})
    page_id = made.json()["page"]["id"]
    real_get = page_routes.config.get
    monkeypatch.setattr(page_routes.config, "get",
                        lambda key, default=None: "Hilltop" if key == "app_name" else real_get(key, default))

    old = c.get(f"/pages/{page_id}", headers={**OWNER, "user-agent": BROWSER + " HilltopApp"})
    assert old.status_code == 403 and old.text == pages.NOTICE_HTML and marker not in old.text
    for name, value in pages.NOTICE_HEADERS.items():
        assert old.headers[name].startswith(value)
    for agent in (BROWSER + " HilltopApp CompanionSealed/1", BROWSER):
        got = c.get(f"/pages/{page_id}", headers={**OWNER, "user-agent": agent})
        assert got.status_code == 200 and marker in got.text
    gone = c.get("/pages/does-not-exist", headers={**OWNER, "user-agent": BROWSER + " HilltopApp"})
    assert gone.status_code == 404
