"""One headless `claude -p` run. The prompt goes on stdin: --allowedTools is
variadic and swallows a positional prompt after it."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def binary() -> str:
    found = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if found:
        return found
    # systemd's user PATH has no ~/.local/bin
    return str(Path.home() / ".local" / "bin" / "claude")


# Everything a headless run may NOT do, whatever the owner's settings say
# (defaultMode auto or bypassPermissions would otherwise let them run).
_GMAIL = "mcp__claude_ai_Gmail__"
DENIED_SERVERS = ["mcp__claude_ai_Google_Calendar", "mcp__claude_ai_Google_Drive"]
# The ONLY built-in tool a run gets. Gmail's tools are deferred, so ToolSearch loads them.
BUILTIN_TOOLS = "ToolSearch"
DENIED_TOOLS = [
    "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent",
    *(_GMAIL + t for t in (
        "send_message", "reply", "forward", "trash_message", "trash_thread", "untrash_message",
        "untrash_thread", "delete_draft", "update_draft", "delete_label", "create_label", "update_label",
        "label_message", "label_thread", "unlabel_message", "unlabel_thread", "update_message_labels",
        "mark_message_spam", "mark_thread_spam", "unmark_message_spam", "unmark_thread_spam",
        "apply_sensitive_message_label", "apply_sensitive_thread_label")),
]
# Only what claude needs to find its login; no other secret is passed down.
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME", "TERM", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
             "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")


def _configured_servers() -> list[str]:
    """Every user-level MCP server the owner has set up, as a deny prefix: none of them
    belongs in a mail run, and the list is read, not named, so no one's setup is in the code."""
    try:
        import json
        d = json.loads((Path.home() / ".claude.json").read_text())
        return [f"mcp__{n}" for n in (d.get("mcpServers") or {})]
    except (OSError, ValueError, AttributeError):
        return []


def build_command(allowed=(), model: str | None = None) -> list[str]:
    cmd = [binary(), "-p", "--model", model or str(config.get("mail_model", DEFAULT_MODEL)),
           "--permission-mode", "default",
           # no user/project/local settings: the owner's defaultMode, hooks and allow rules never apply.
           # (--strict-mcp-config is NOT used: it drops the claude.ai Gmail connector, checked live.)
           "--setting-sources", "",
           "--tools", BUILTIN_TOOLS]
    # a denied tool never rides in the allowed list, whatever a caller passes
    allowed = [t for t in allowed if t not in DENIED_TOOLS]
    if allowed:
        cmd += ["--allowedTools", *allowed]
    # --disallowedTools is variadic too; the prompt is on stdin so nothing follows it
    # whole servers by name prefix: the rule matches every tool the server has
    servers = list(dict.fromkeys([*DENIED_SERVERS, *_configured_servers()]))
    cmd += ["--disallowedTools", *DENIED_TOOLS, *servers]
    return cmd


def build_env() -> dict:
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env["AGENT_COMPANION_JOB"] = "mail"
    return env


def run(prompt: str, allowed=(), timeout: int = 300, model: str | None = None) -> str:
    """The model's whole answer. Raises RuntimeError on a failed run.

    `allowed` is the complete tool list for the run. Empty means no tool is
    named at all; a headless run has nobody to approve anything else."""
    cmd, env = build_command(allowed, model), build_env()
    try:
        with tempfile.TemporaryDirectory(prefix="cc-mail-") as cwd:   # fresh and empty: nothing to read here
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                               env=env, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"claude did not run: {exc}") from exc
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[-200:]}")
    return p.stdout
