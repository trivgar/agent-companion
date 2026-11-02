import pytest

pytest.importorskip("fastapi")
from fastapi import APIRouter, FastAPI  # noqa: E402

from agent_companion.server.walk import all_routes  # noqa: E402


class _Included:
    """How FastAPI after 0.135 keeps an include in app.routes."""

    def __init__(self, router):
        self.original_router = router


def test_the_walk_goes_into_included_routers_however_deep():
    inner, outer = APIRouter(), APIRouter()

    @inner.get("/api/inner")
    def a():
        return {}

    @outer.get("/api/outer")
    def b():
        return {}
    outer.routes.append(_Included(inner))
    app = FastAPI()
    app.router.routes.append(_Included(outer))
    found = all_routes(app)
    assert {"/api/inner", "/api/outer"} <= {getattr(r, "path", None) for r in found}
    assert not [r for r in found if isinstance(r, _Included)]
