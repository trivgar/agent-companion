"""The test emulator's decisions (app/phoneemu.py). Pure: nothing here runs
adb, systemd or the emulator."""
from pathlib import Path
import time

from agent_companion.phone import emu as E


def test_kvm_state():
    assert E.kvm_state(True, True, False) == "ok"
    assert E.kvm_state(True, False, True) == "sg"
    assert E.kvm_state(True, False, False) == "none"
    assert E.kvm_state(False, False, True) == "none"


def test_mem_available_kb():
    assert E.mem_available_kb("MemTotal: 19000000 kB\nMemAvailable:   9123456 kB\n") == 9123456
    assert E.mem_available_kb("nothing") == 0


def test_refusal_says_why_in_order():
    eight = 8 * 1024 * 1024
    assert "gaming" in E.refusal("gaming", eight * 2, "ok")
    assert "hot" in E.refusal("hot", eight * 2, "ok")
    low = E.refusal(None, 6 * 1024 * 1024, "ok")
    assert "6.0 GB" in low and "8" in low
    assert "KVM" in E.refusal(None, eight, "none")
    assert E.refusal(None, eight, "ok") is None
    assert E.refusal(None, eight, "sg") is None
    assert "\u2014" not in E.refusal("gaming", 0, "none")


def test_the_emulator_runs_headless_on_its_own_port_with_a_2_gb_guest():
    argv = E.emulator_argv(Path("/sdk"))
    assert argv[0] == "/sdk/emulator/emulator"
    for flag in ("-no-window", "-no-audio", "-no-boot-anim", "-no-metrics", "-no-snapshot-load", "-no-snapshot-save"):
        assert flag in argv
    assert argv[argv.index("-avd") + 1] == "agent-companion-test"
    assert argv[argv.index("-port") + 1] == "5580"
    assert argv[argv.index("-gpu") + 1] == "swiftshader_indirect"
    assert argv[argv.index("-memory") + 1] == "2048"
    assert E.SERIAL == "emulator-5580"


def test_the_unit_is_capped_and_wraps_sg_only_when_the_login_lacks_kvm():
    emu = ["/sdk/emulator/emulator", "-avd", "agent-companion-test"]
    plain = E.unit_argv(emu, "ok", Path("/sdk"))
    assert plain[:5] == ["systemd-run", "--user", "--unit=phone-emulator", "--collect", "-p"]
    assert "MemoryMax=5G" in plain and "MemorySwapMax=1G" in plain
    assert "--setenv=ANDROID_HOME=/sdk" in plain
    assert plain[-3:] == emu
    sg = E.unit_argv(emu, "sg", Path("/sdk"))
    assert sg[-4:] == ["sg", "kvm", "-c", "/sdk/emulator/emulator -avd agent-companion-test"]


def test_the_watcher_is_its_own_unit_running_this_repos_phone_command():
    argv = E.watch_argv("/usr/bin/python3", Path("/repo"))
    assert argv == ["systemd-run", "--user", "--unit=phone-emulator-watch", "--collect",
                    "/usr/bin/python3", "/repo/bin/phone", "emu", "watch"]


def test_watch_step():
    now = 10_000.0
    fresh = now - 60
    assert E.watch_step(None, 0, now, fresh, False, False) == "stop-lease"
    assert E.watch_step(None, 0, now, now - E.IDLE_S - 1, True, False) == "stop-idle"
    assert E.watch_step(None, 0, now, None, True, False) == "stop-idle"
    assert E.watch_step("gaming", 2, now, fresh, True, False) == "pin"
    assert E.watch_step("gaming", 2, now, fresh, True, True) == "keep"
    assert E.watch_step(None, 0, now, fresh, True, True) == "unpin"
    assert E.watch_step("hot", 0, now, fresh, True, True) == "unpin"
    assert E.watch_step(None, 0, now, fresh, True, False) == "keep"
    assert E.watch_step("gaming", 0, now, fresh, True, False) == "keep"


def test_pinned_set_is_the_last_n_cpus_as_heavy_picks_them():
    assert E.pinned_set(range(28), 2) == {26, 27}
    assert E.pinned_set([3, 1, 2], 1) == {3}
    assert E.pinned_set([0, 1], 0) == {0, 1}


def test_badging():
    text = ("package: name='com.agent-companion.app' versionCode='812' versionName='1.812-emu' "
            "platformBuildVersionName='15'\nsdkVersion:'26'\n")
    assert E.badging(text) == ("com.agent-companion.app", "1.812-emu")
    assert E.badging("garbage") == ("", "")


def test_a_normal_build_never_goes_on_the_emulator():
    refused = E.apk_refusal("com.agentcompanion.app", "1.812")
    assert refused and "comms" in refused and "--emulator" in refused
    assert E.apk_refusal("com.agentcompanion.app", "1.812-emu") is None
    assert E.apk_refusal("com.example.otherapp", "2.3.1") is None
    assert "package" in E.apk_refusal("", "")


def test_prompt_point_reads_the_newest_prompt_line():
    log = ("11-16 06:00:01.000 I/agent-companionDriver( 123): PROMPT allow=800,2000 deny=280,2000\n"
           "11-16 06:00:09.000 I/agent-companionDriver( 123): PROMPT gone\n")
    assert E.prompt_point(log, "allow") is None
    log += "11-16 06:01:00.000 I/agent-companionDriver( 123): PROMPT allow=810,1990 deny=270,1990\n"
    assert E.prompt_point(log, "allow") == (810, 1990)
    assert E.prompt_point(log, "deny") == (270, 1990)
    assert E.prompt_point("", "allow") is None


def test_banner_point_reads_each_button_and_the_last_line_wins():
    log = ("I/agent-companionDriver( 1): BANNER stop=1000,80\n"
           "I/agent-companionDriver( 1): BANNER continue=800,80\n"
           "I/agent-companionDriver( 1): BANNER resume=700,80\n"
           "I/agent-companionDriver( 1): BANNER stop=990,82\n"
           "I/agent-companionDriver( 1): BANNER continue=790,82\n"
           "I/agent-companionDriver( 1): BANNER resume=690,82\n")
    assert E.banner_point(log, "stop") == (990, 82)
    assert E.banner_point(log, "continue") == (790, 82)
    assert E.banner_point(log, "resume") == (690, 82)


def test_banner_point_refuses_continue_after_its_offer_is_gone_and_both_after_banner_gone():
    log = ("I/agent-companionDriver( 1): BANNER stop=1000,80\n"
           "I/agent-companionDriver( 1): BANNER continue=800,80\n"
           "I/agent-companionDriver( 1): BANNER offer=none\n")
    assert E.banner_point(log, "continue") is None
    assert E.banner_point(log, "resume") is None
    assert E.banner_point(log, "stop") == (1000, 80)
    assert E.banner_point(log + "I/agent-companionDriver( 1): BANNER gone\n", "continue") is None
    assert E.banner_point(log + "I/agent-companionDriver( 1): BANNER gone\n", "resume") is None
    assert E.banner_point(log + "I/agent-companionDriver( 1): BANNER gone\n", "stop") is None


def test_input_arg_quotes_for_the_emulators_shell():
    assert E.input_arg("demo pass") == "'demo%spass'"
    assert E.input_arg("it's") == "'it'\\''s'"
    for bad in ("", "100%", "café", "two\nlines"):
        try:
            E.input_arg(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


import subprocess

import pytest


class Fake:
    """A runner: records each argv and answers by the longest matching prefix."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, timeout=60):
        self.calls.append(list(argv))
        best = None
        for prefix, reply in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, reply)
        rc, out = best[1] if best else (0, "")
        if callable(out):
            out = out()
        return subprocess.CompletedProcess(argv, rc, out, "")

    def ran(self, *prefix):
        return [c for c in self.calls if tuple(c[:len(prefix)]) == prefix]


ACTIVE = ("systemctl", "--user", "is-active", "phone-emulator")
WATCH_ACTIVE = ("systemctl", "--user", "is-active", "phone-emulator-watch")
BOOTED = ("adb", "-s", "emulator-5580", "shell", "getprop", "sys.boot_completed")


@pytest.fixture
def phone_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(E, "DATA", tmp_path)
    monkeypatch.setattr(E, "USED", tmp_path / "emu-used")
    return tmp_path


@pytest.fixture
def roomy(monkeypatch):
    monkeypatch.setattr(E.loadguard, "holding", lambda: None)
    monkeypatch.setattr(E, "read_mem_kb", lambda: 12 * 1024 * 1024)
    monkeypatch.setattr(E, "read_kvm", lambda: "ok")


def test_run_never_raises():
    r = E.run(["/no/such/binary-anywhere"], timeout=5)
    assert r.returncode == 127


def test_start_refuses_while_he_games_and_starts_nothing(phone_dir, roomy, monkeypatch):
    monkeypatch.setattr(E.loadguard, "holding", lambda: "gaming")
    fake = Fake({ACTIVE: (3, "inactive")})
    ok, words = E.start(fake, clock=lambda: 0.0, wait=lambda s: None)
    assert not ok and "gaming" in words
    assert not fake.ran("systemd-run")


def test_start_boots_the_unit_and_the_watcher_and_waits_for_android(phone_dir, roomy):
    t = [0.0]
    states = iter(["inactive", "activating", "active", "active", "active"])
    watchers = iter(["inactive", "active", "active"])
    boot = iter(["", "", "1"])
    fake = Fake({ACTIVE: (0, lambda: next(states)), WATCH_ACTIVE: (0, lambda: next(watchers)),
                 BOOTED: (0, lambda: next(boot))})
    ok, words = E.start(fake, clock=lambda: t[0], wait=lambda s: t.__setitem__(0, t[0] + s))
    assert ok and "emulator-5580" in words
    units = fake.ran("systemd-run")
    assert units[0][2] == "--unit=phone-emulator" and units[1][2] == "--unit=phone-emulator-watch"
    assert E.used_at() is not None


def test_start_stamps_its_use_before_the_watcher_first_looks(phone_dir, roomy):
    E.touch_used(now=1.0)                       # the last session's stamp, long past IDLE_S
    t = [0.0]
    seen = {}

    def the_watcher_starts():
        seen["used"] = E.used_at()
        return ""
    states = iter(["inactive", "activating", "active", "active", "active"])
    watchers = iter(["inactive", "active", "active"])
    boot = iter(["", "", "1"])
    fake = Fake({ACTIVE: (0, lambda: next(states)), WATCH_ACTIVE: (0, lambda: next(watchers)),
                 BOOTED: (0, lambda: next(boot)),
                 ("systemd-run", "--user", "--unit=phone-emulator-watch"): (0, the_watcher_starts)})
    ok, _ = E.start(fake, clock=lambda: t[0], wait=lambda s: t.__setitem__(0, t[0] + s))
    assert ok
    assert E.watch_step(None, 0, time.time(), seen["used"], True, False) == "keep", \
        "the watcher's first look must see a fresh stamp"


def test_start_that_never_boots_stops_it_and_says_so(phone_dir, roomy):
    t = [0.0]
    states = iter(["inactive"])               # down when asked first, then up forever
    watchers = iter(["inactive", "active"])
    fake = Fake({ACTIVE: (0, lambda: next(states, "active")),
                 WATCH_ACTIVE: (0, lambda: next(watchers, "active")), BOOTED: (0, "")})
    ok, words = E.start(fake, clock=lambda: t[0], wait=lambda s: t.__setitem__(0, t[0] + s))
    assert not ok and "did not finish booting" in words
    assert fake.ran("adb", "-s", "emulator-5580", "emu", "kill")


def test_start_when_already_up_launches_nothing(phone_dir, roomy):
    fake = Fake({ACTIVE: (0, "active"), WATCH_ACTIVE: (0, "active"), BOOTED: (0, "1")})
    ok, words = E.start(fake, clock=lambda: 0.0, wait=lambda s: None)
    assert ok and not fake.ran("systemd-run")


def test_start_stops_the_emulator_when_the_watcher_will_not_start(phone_dir, roomy):
    states = iter(["inactive", "active", "inactive"])
    fake = Fake({ACTIVE: (0, lambda: next(states, "inactive")), WATCH_ACTIVE: (3, "inactive"),
                 ("systemd-run", "--user", "--unit=phone-emulator-watch"): (1, "failed")})
    ok, words = E.start(fake, clock=lambda: 0.0, wait=lambda s: None)
    assert not ok and "watcher" in words
    assert fake.ran("adb", "-s", "emulator-5580", "emu", "kill")
    assert not fake.ran(*BOOTED)


def test_start_stops_the_emulator_when_the_new_watcher_dies_at_once(phone_dir, roomy):
    states = iter(["inactive", "active", "inactive"])
    waits = []
    fake = Fake({ACTIVE: (0, lambda: next(states, "inactive")), WATCH_ACTIVE: (3, "inactive")})
    ok, words = E.start(fake, clock=lambda: 0.0, wait=waits.append)
    assert not ok and "watcher" in words
    assert fake.ran("adb", "-s", "emulator-5580", "emu", "kill")
    assert E.WATCH_GRACE_S in waits


def test_start_gives_an_emulator_that_is_up_a_new_watcher_when_its_own_is_gone(phone_dir, roomy):
    watchers = iter(["inactive", "active"])
    fake = Fake({ACTIVE: (0, "active"), WATCH_ACTIVE: (0, lambda: next(watchers, "active")),
                 BOOTED: (0, "1")})
    ok, words = E.start(fake, clock=lambda: 0.0, wait=lambda s: None)
    units = fake.ran("systemd-run")
    assert ok and "emulator-5580" in words
    assert len(units) == 1 and units[0][2] == "--unit=phone-emulator-watch"


def test_start_that_boots_without_its_watcher_stops_it(phone_dir, roomy):
    states = iter(["inactive", "active", "inactive"])
    watchers = iter(["inactive", "active", "inactive"])
    fake = Fake({ACTIVE: (0, lambda: next(states, "inactive")),
                 WATCH_ACTIVE: (0, lambda: next(watchers, "inactive")), BOOTED: (0, "1")})
    ok, words = E.start(fake, clock=lambda: 0.0, wait=lambda s: None)
    assert not ok and "watcher" in words
    assert fake.ran("adb", "-s", "emulator-5580", "emu", "kill")


def test_install_refuses_his_build_before_adb(phone_dir, tmp_path, monkeypatch):
    apk = tmp_path / "agent-companion.apk"
    apk.write_bytes(b"PK")
    monkeypatch.setattr(E, "aapt_path", lambda sdk=E.SDK: Path("/sdk/aapt"))
    fake = Fake({("/sdk/aapt",): (0, "package: name='com.agentcompanion.app' versionCode='9' versionName='1.9'")})
    ok, words = E.install(str(apk), fake)
    assert not ok and "comms" in words
    assert not fake.ran("adb")


def test_install_puts_an_emu_build_on_the_emulator_only(phone_dir, tmp_path, monkeypatch):
    apk = tmp_path / "agent-companion-emu.apk"
    apk.write_bytes(b"PK")
    monkeypatch.setattr(E, "aapt_path", lambda sdk=E.SDK: Path("/sdk/aapt"))
    fake = Fake({("/sdk/aapt",): (0, "package: name='com.agentcompanion.app' versionCode='9' versionName='1.9-emu'"),
                 BOOTED: (0, "1"),
                 ("adb", "-s", "emulator-5580", "install"): (0, "Performing Streamed Install\nSuccess\n")})
    ok, words = E.install(str(apk), fake)
    assert ok and "1.9-emu" in words and "notification permission" in words
    assert fake.ran("adb", "-s", "emulator-5580", "install", "-r", "-t", str(apk))
    assert fake.ran("adb", "-s", "emulator-5580", "shell",
                    "pm grant com.agentcompanion.app android.permission.POST_NOTIFICATIONS")


def test_every_adb_call_names_the_emulator(phone_dir, roomy, tmp_path, monkeypatch):
    monkeypatch.setattr(E, "aapt_path", lambda sdk=E.SDK: Path("/sdk/aapt"))
    log = "I/agent-companionDriver( 1): PROMPT allow=800,2000 deny=280,2000\n"
    fake = Fake({ACTIVE: (0, "active"), BOOTED: (0, "1"),
                 ("adb", "-s", "emulator-5580", "logcat"): (0, log),
                 ("/sdk/aapt",): (0, "package: name='x.y' versionName='1'"),
                 ("adb", "-s", "emulator-5580", "install"): (0, "Success")})
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    E.install(str(apk), fake)
    E.reset(fake)
    E.save_clean(fake)
    E.logs(50, fake)
    E.answer("allow", fake)
    E.type_text("demo pass", fake)
    E.stop(fake, wait=lambda s: None)
    adb_calls = [c for c in fake.calls if c[0] == "adb"]
    assert adb_calls and all(c[1:3] == ["-s", "emulator-5580"] for c in adb_calls)


def test_answer_touches_the_prompts_button_or_says_none_is_up(phone_dir):
    up = "I/agent-companionDriver( 1): PROMPT allow=800,2000 deny=280,2000\n"
    fake = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "logcat"): (0, up)})
    ok, words = E.answer("deny", fake)
    assert ok and "Deny" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "input", "tap", "280", "2000")
    gone = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "logcat"): (0, up + "I/agent-companionDriver( 1): PROMPT gone\n")})
    ok, words = E.answer("allow", gone)
    assert not ok and "no prompt" in words
    assert not gone.ran("adb", "-s", "emulator-5580", "shell", "input")


def test_answer_touches_banner_buttons_and_refuses_stale_ones(phone_dir):
    up = ("I/agent-companionDriver( 1): BANNER stop=1000,80\n"
          "I/agent-companionDriver( 1): BANNER continue=800,80\n"
          "I/agent-companionDriver( 1): BANNER resume=700,80\n")
    fake = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "logcat"): (0, up)})
    ok, words = E.answer("continue", fake)
    assert ok and "Continue" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "input", "tap", "800", "80")
    ok, words = E.answer("resume", fake)
    assert ok and "Resume" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "input", "tap", "700", "80")
    ok, words = E.answer("stop", fake)
    assert ok and "Stop" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "input", "tap", "1000", "80")
    no_continue = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "logcat"): (0, up + "BANNER offer=none\n")})
    ok, words = E.answer("continue", no_continue)
    assert not ok and "no Continue offer" in words
    ok, words = E.answer("resume", no_continue)
    assert not ok and "no Resume offer" in words
    no_stop = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "logcat"): (0, up + "BANNER gone\n")})
    ok, words = E.answer("stop", no_stop)
    assert not ok and "no driving banner" in words


def test_answer_notice_stop_uses_only_a_notice_logged_after_the_shade_expands(phone_dir):
    old = "I/agent-companionDriver( 1): NOTICE stop=1000,800\n"
    fresh = old + "I/agent-companionDriver( 1): NOTICE stop=900,700\n"
    logcats = iter([old, fresh])
    fake = Fake({BOOTED: (0, "1"),
                 ("adb", "-s", "emulator-5580", "logcat"): (0, lambda: next(logcats))})
    ok, words = E.answer("notice-stop", fake, clock=lambda: 0.0, wait=lambda seconds: None)
    assert ok and "notification Stop" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "cmd statusbar expand-notifications")
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "input tap 900 700")
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "cmd statusbar collapse")


def test_answer_notice_stop_refuses_a_notice_stale_before_the_shade_expands(phone_dir):
    old = "I/agent-companionDriver( 1): NOTICE stop=1000,800\n"
    now = [0.0]
    fake = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "logcat"): (0, old)})
    ok, words = E.answer("notice-stop", fake, clock=lambda: now[0],
                         wait=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert not ok and "no notification Stop" in words
    assert not fake.ran("adb", "-s", "emulator-5580", "shell", "input tap")
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "cmd statusbar collapse")


@pytest.mark.parametrize(("what", "arg", "commands"), [
    ("power", "", ["input keyevent KEYCODE_SLEEP"]),
    ("wake", "", ["input keyevent KEYCODE_WAKEUP"]),
    ("home", "", ["input keyevent KEYCODE_HOME"]),
    ("lock", "", ["locksettings set-disabled false", "input keyevent KEYCODE_SLEEP",
                     "input keyevent KEYCODE_WAKEUP"]),
    ("unlock", "", ["wm dismiss-keyguard"]),
    ("lock-off", "", ["locksettings set-disabled true"]),
    ("call", "", None),
    ("accept", "", None),
    ("hangup", "", None),
    ("touch", "12,34", ["input tap 12 34"]),
])
def test_users_hands_use_the_measured_adb_commands(phone_dir, what, arg, commands):
    fake = Fake({BOOTED: (0, "1")})
    ok, _ = E.user(what, arg, fake)
    assert ok
    if commands is not None:
        assert fake.calls[1:] == [["adb", "-s", E.SERIAL, "shell", command] for command in commands]
    else:
        verb = {"call": "call", "accept": "accept", "hangup": "cancel"}[what]
        assert fake.calls[1:] == [["adb", "-s", E.SERIAL, "emu", "gsm", verb, "5551234"]]


@pytest.mark.parametrize("arg", ["", "12", "12,", "12,34,56", "-1,0", "0,-1", "4001,0", "0,4001"])
def test_users_touch_refuses_anything_but_a_point_on_the_emulator(phone_dir, arg):
    fake = Fake({BOOTED: (0, "1")})
    ok, words = E.user("touch", arg, fake)
    assert not ok and "X,Y" in words
    assert not fake.ran("adb", "-s", E.SERIAL, "shell", "input tap")


def test_chrome_page_and_messages_draft_quote_for_the_emulators_shell(phone_dir):
    # adb joins a shell command's arguments with spaces and the emulator's shell
    # parses the line again: `sh -c "<script>"` would run only the script's first word.
    fake = Fake({BOOTED: (0, "1")})
    ok, words = E.chrome_page(fake)
    assert ok and "Chrome" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell",
                    "echo '" + E.CHROME_COMMAND_LINE + "' > /data/local/tmp/chrome-command-line")
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "am", "start", "-a",
                    "android.intent.action.VIEW", "-d", E.E2E_PAGE, E.CHROME)
    ok, words = E.messages_draft(fake)
    assert ok and "Messages" in words
    assert fake.ran("adb", "-s", "emulator-5580", "shell", "am", "start", "-a", "android.intent.action.SENDTO",
                    "-d", "sms:5551234", "--es", "sms_body", "'On my way'")
    assert not fake.ran("adb", "-s", "emulator-5580", "shell", "sh")


def test_messages_code_sends_the_fixed_sms_then_opens_its_thread(phone_dir):
    fake = Fake({BOOTED: (0, "1")})
    ok, words = E.messages_code(fake)
    assert ok and "verification code" in words
    assert fake.calls[1:] == [
        ["adb", "-s", E.SERIAL, "emu", "sms", "send", "5551234",
         "G-482913 is your Google verification code."],
        ["adb", "-s", E.SERIAL, "shell",
         "am start -a android.intent.action.SENDTO -d sms:5551234"],
    ]


def test_reset_says_how_to_make_the_clean_snapshot(phone_dir):
    fake = Fake({BOOTED: (0, "1"), ("adb", "-s", "emulator-5580", "emu", "avd", "snapshot", "load"): (0, "KO: snapshot not found")})
    ok, words = E.reset(fake)
    assert not ok and "save-clean" in words


def test_reset_waits_until_adb_has_the_emulator_back(phone_dir):
    answers = iter(["1", "", "", "1"])           # up, then gone after the load, then back
    fake = Fake({BOOTED: (0, lambda: next(answers, "1"))})
    slept = []
    ok, words = E.reset(fake, wait=slept.append)
    assert ok and words == "back to the clean snapshot"
    assert slept == [2, 2]


def test_reset_says_so_when_the_emulator_never_comes_back(phone_dir):
    answers = iter(["1"])
    fake = Fake({BOOTED: (0, lambda: next(answers, ""))})
    ok, words = E.reset(fake, wait=lambda s: None)
    assert not ok and "did not answer again" in words


def test_chores_need_the_emulator_up(phone_dir):
    fake = Fake({BOOTED: (1, "")})
    for ok, words in (E.reset(fake), E.save_clean(fake), E.logs(10, fake), E.answer("allow", fake),
                      E.type_text("x", fake), E.user("power", runner=fake), E.messages_code(fake)):
        assert not ok and "phone emu start" in words


def test_the_watcher_stops_an_idle_emulator(phone_dir, monkeypatch):
    E.touch_used(now=1000.0)
    monkeypatch.setattr(E.leases, "live", lambda device, *a, **k: {"holder": "agent-one"})
    monkeypatch.setattr(E.loadguard, "holding", lambda: None)
    monkeypatch.setattr(E.loadguard, "read", lambda p: {})
    alive = iter(["active", "active"])        # up for the watcher's look and stop's first, then gone
    fake = Fake({ACTIVE: (0, lambda: next(alive, "inactive"))})
    assert E.watch(fake, clock=lambda: 1000.0 + E.IDLE_S + 5, wait=lambda s: None) == "stop-idle"
    assert fake.ran("adb", "-s", "emulator-5580", "emu", "kill")
    assert not fake.ran("systemctl", "--user", "stop", "phone-emulator-watch")


def test_the_watcher_pins_while_he_games(phone_dir, monkeypatch):
    E.touch_used()
    monkeypatch.setattr(E.leases, "live", lambda device, *a, **k: {"holder": "agent-one"})
    monkeypatch.setattr(E.loadguard, "holding", lambda: "gaming")
    monkeypatch.setattr(E.loadguard, "read", lambda p: {"state": "gaming", "cpus": 2})
    pinned = []
    monkeypatch.setattr(E, "pin", lambda n, runner=None: pinned.append(n))
    monkeypatch.setattr(E, "unpin", lambda runner=None: pinned.append("off"))
    fake = Fake({ACTIVE: (0, "active")})
    assert E.watch(fake, wait=lambda s: None, once=True) == "pin"
    assert pinned == [2]


def test_unit_pids_never_reads_the_root_cgroup(monkeypatch):
    fake = Fake({("systemctl", "--user", "show"): (0, "\n")})
    assert E.unit_pids(fake) == []
