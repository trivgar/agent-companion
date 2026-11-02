import os

# Never the real config: a test reads only what it sets. AGENT_COMPANION_LEAK_TERMS
# is kept: it points the leak check at a deny list, as tests/test_leaks.py says.
os.environ["AGENT_COMPANION_CONFIG"] = os.path.join(os.path.dirname(__file__), "no-such-config.toml")
for k in [k for k in os.environ if k.startswith(("AGENT_COMPANION_", "CLAUDE_COMPANION_"))
          and k not in ("AGENT_COMPANION_CONFIG", "AGENT_COMPANION_LEAK_TERMS")]:
    del os.environ[k]

# Owner keys and pairing codes live in the test's own folder, never the real config.
import site  # noqa: E402
import tempfile  # noqa: E402
_cfg = tempfile.mkdtemp(prefix="cc-test-cfg-")
os.environ["AGENT_COMPANION_OWNER_KEYS"] = os.path.join(_cfg, "owner-keys.json")
os.environ["AGENT_COMPANION_PAIRING"] = os.path.join(_cfg, "pairing-codes.json")
os.environ["AGENT_COMPANION_ALLOWED_HOSTS"] = "testserver"   # TestClient sends Host: testserver

# Never the real home either. A blank settings file still left every default
# path in the machine's own home (data_dir, ~/.claude, ~/.claude/tts, the push
# store), and on 2026-11-01 a suite run wrote the real
# ~/.local/share/agent-companion/chat-seen.json. A Python a test starts still
# finds the packages installed for the real user.
os.environ.setdefault("PYTHONUSERBASE", site.getuserbase())
# The page tests' browsers stay where they were installed. Without this every
# page test skips as "chromium unavailable" in the new home, and the suite
# still reads green (two settings page tests that move HOME themselves did
# exactly that until 2026-11-01).
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"), "ms-playwright"))
# So does the leak check's deny list, which lives outside every repo: in the
# new home it would find none and pass on the generic patterns alone.
os.environ.setdefault("AGENT_COMPANION_LEAK_TERMS",
                      os.path.join(os.path.expanduser("~"), ".config", "agent-companion", "leak-terms"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="cc-test-home-")
for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "CLAUDE_HOME"):
    os.environ.pop(k, None)
