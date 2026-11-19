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
