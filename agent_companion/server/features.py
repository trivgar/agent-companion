"""The features the server loads, in nav order.

Each is a module in this package with `router` (a FastAPI APIRouter), and
optionally `TITLE` and `start(app)` for background work. A module that is not
there yet is skipped with a log line, so a branch without a feature still runs.
The page loads /static/<id>.js for each one that loaded.
"""
from __future__ import annotations

FEATURES: list[str] = ["sessions", "health", "scoreboard", "files", "specs", "pages", "asks", "chat", "mail", "money", "fitness", "deferred", "comms", "voicelab", "connections", "settings", "push", "phone"]
