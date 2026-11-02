import sys
import types

from agent_companion import addons


def _plug(monkeypatch, **fns):
    mod = types.ModuleType("fake_addons")
    for k, v in fns.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, "fake_addons", mod)
    monkeypatch.setenv("AGENT_COMPANION_ADDONS", "fake_addons")
    addons.load.cache_clear()


def test_no_module_gives_the_defaults(monkeypatch):
    monkeypatch.delenv("AGENT_COMPANION_ADDONS", raising=False)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", "/nonexistent")
    addons.load.cache_clear()
    assert addons.person(object()) is None
    assert addons.asleep() is False
    assert addons.comms_busy() is False
    assert addons.private("/home/sam/work", "work") is False
    assert addons.chat_target({"name": "Ada"}) is True
    assert addons.chat_characters({"name": "Ada"}) == []
    assert addons.chat_action("end", {"name": "Ada"}, {}) is None
    assert addons.voice_host_status("main", None, 0.0) == {}
    assert addons.tts_fix("undelivered") is None
    addons.on_ask("created", {"id": "a"})


def test_a_plugin_is_called(monkeypatch):
    seen = []
    _plug(monkeypatch, person=lambda r: {"name": "alex"}, asleep=lambda: True, comms_busy=lambda: True,
          on_ask=lambda e, a: seen.append((e, a["id"])), private=lambda cwd, p: p == "diary")
    assert addons.person(object()) == {"name": "alex"}
    assert addons.asleep() is True
    assert addons.comms_busy() is True
    assert addons.private("/x/diary", "diary") is True
    addons.on_ask("answered", {"id": "a1"})
    assert seen == [("answered", "a1")]


def test_on_ask_returns_only_a_dict_from_the_plugin(monkeypatch):
    _plug(monkeypatch, on_ask=lambda e, a: {"pushed": True})
    assert addons.on_ask("created", {"id": "a"}) == {"pushed": True}
    _plug(monkeypatch, on_ask=lambda e, a: "ignored")
    assert addons.on_ask("created", {"id": "a"}) is None


def test_a_broken_plugin_fails_safe(monkeypatch):
    def boom(*a):
        raise RuntimeError("no")
    _plug(monkeypatch, person=boom, asleep=boom, comms_busy=boom, on_ask=boom, private=boom,
          chat_target=boom, chat_characters=boom, chat_action=boom, voice_host_status=boom, tts_fix=boom)
    assert addons.person(object()) is None
    assert addons.asleep() is True
    assert addons.comms_busy() is True
    assert addons.private("/x/y", "y") is True
    assert addons.chat_target({"name": "Ada"}) is False
    assert addons.chat_characters({"name": "Ada"}) == []
    assert addons.chat_action("end", {"name": "Ada"}, {}) is None
    assert addons.voice_host_status("main", None, 0.0) == {}
    assert addons.tts_fix("silent") is None
    addons.on_ask("created", {"id": "a"})


def test_repair_steps_are_four_texts_whatever_the_plugin_returns(monkeypatch):
    _plug(monkeypatch, tts_fix=lambda kind: {"what": kind, "cmd": 7, "after": None, "html": "<b>"})
    assert addons.tts_fix("silent") == {"what": "silent", "where": "", "cmd": "7", "after": ""}
    _plug(monkeypatch, tts_fix=lambda kind: "restart it")
    assert addons.tts_fix("silent") is None


def test_a_chat_plugin_can_narrow_a_known_target_and_add_actions(monkeypatch):
    _plug(monkeypatch, chat_target=lambda target: target["name"] != "Private",
          chat_characters=lambda target: [{"name": "Nova", "custom": True}],
          chat_action=lambda action, target, body: {"ok": action == "end", "name": target["name"]})
    assert addons.chat_target({"name": "Private"}) is False
    assert addons.chat_target({"name": "Ada"}) is True
    assert addons.chat_characters({"name": "Ada"}) == [{"name": "Nova", "custom": True}]
    assert addons.chat_action("end", {"name": "Ada"}, {}) == {"ok": True, "name": "Ada"}


def test_a_chat_target_plugin_cannot_rewrite_its_target(monkeypatch):
    def narrow(target):
        target["pid"] = 99
        target["pane"] = "%99"
        return True

    target = {"name": "Ada", "pid": 11, "pane": "%3"}
    _plug(monkeypatch, chat_target=narrow)
    assert addons.chat_target(target) is True
    assert target == {"name": "Ada", "pid": 11, "pane": "%3"}

    _plug(monkeypatch, chat_target=lambda target: "yes")
    assert addons.chat_target(target) is False


def test_the_config_list_stays_private_whatever_the_plugin_says(monkeypatch, tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('private_projects = ["ledger"]\n')
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(f))
    _plug(monkeypatch, private=lambda cwd, p: False)
    assert addons.private("/home/sam/ledger", "ledger") is True
    assert addons.private("/home/sam/ledger", "") is True
    assert addons.private("/home/sam/ledger/receipts", "receipts") is True
    assert addons.private("/home/sam/site", "site") is False


def test_a_missing_module_is_the_default_not_a_crash(monkeypatch):
    monkeypatch.setenv("AGENT_COMPANION_ADDONS", "no_such_addons_module")
    addons.load.cache_clear()
    assert addons.load() is None
    assert addons.person(object()) is None
