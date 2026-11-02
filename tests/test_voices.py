"""Real voices or the backup ones. Remote voice hosts are configured data,
never built-in machines: every host serving a voice in use is asked at once,
the newest remote clip in speak.log counts too, and the page reads one line."""
import sys
import threading
import time
import types

import pytest

from agent_companion import addons, voices

A, B = "http://a", "http://b"
TWO = f"main={A}\nlocal={B} narrator\n"
ALL = f"main={A}\nlocal={B} *\n"
READY = {"state": "ready", "why": ""}
IN_USE = {"state": "stood_down", "why": "main is in use"}
AWAY = {"state": "stood_down", "why": "local is away"}
NAMES = ('[voice_host_names.local]\nsubject = "the voice host on this PC"\n'
         'possessive = "this PC\'s"\nplace = "this PC"\n')


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    """No add-on, and a settings file that names the local host."""
    path = tmp_path / "settings.toml"
    path.write_text(NAMES)
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(path))
    addons.load.cache_clear()
    voices._cache.clear()
    yield
    addons.load.cache_clear()


def home(tmp_path, log="", hosts=f"main={A}\n", wanted="Robot=robot\nNarrator=narrator\n"):
    tts = tmp_path / "tts"
    tts.mkdir(exist_ok=True)
    (tts / "remote-hosts.conf").write_text(hosts)
    (tts / "remote-voices.conf").write_text(wanted)
    (tts / "speak.log").write_text(log)
    voices._cache.clear()
    return tts


def stamp(when):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def answers(by_url):
    return lambda url: by_url.get(url)


def _plug(monkeypatch, **fns):
    module = types.ModuleType("fake_voice_addons")
    for name, fn in fns.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "fake_voice_addons", module)
    monkeypatch.setenv("AGENT_COMPANION_ADDONS", "fake_voice_addons")
    addons.load.cache_clear()


def test_absent_host_and_cast_files_need_no_network(tmp_path):
    assert voices.hosts(tmp_path) == []
    assert voices.state(tmp_path, now=1, health=lambda url: (_ for _ in ()).throw(AssertionError(url)))["configured"] is False


def test_configured_hosts_report_fallback_without_names_in_code(tmp_path):
    (tmp_path / "remote-hosts.conf").write_text("primary=http://example.invalid\nbackup=http://example.invalid voice\n")
    (tmp_path / "remote-voices.conf").write_text("Speaker=voice\n")
    state = voices.state(tmp_path, now=100, health=lambda url: None)
    assert state["real"] is False and "primary" in state["why"]


def test_a_host_off_is_backup(tmp_path):
    s = voices.state(home(tmp_path), health=lambda url: None)
    assert s["real"] is False and s["why"] == "main is off or out of reach"


def test_a_busy_card_is_backup(tmp_path):
    # a host from before /health had a state
    s = voices.state(home(tmp_path), health=lambda url: {"busy": True})
    assert s["real"] is False and s["why"] == "main's graphics card is busy"


def test_up_and_the_last_clip_real_says_nothing(tmp_path):
    now = time.time()
    s = voices.state(home(tmp_path, f"{stamp(now - 30)} REMOTE(robot@main) 2.1s Robot\n"), now=now,
                     health=lambda url: READY)
    assert s == {"real": True, "why": "", "note": "", "at": now, "configured": True}


def test_a_recent_fallback_shows_until_a_real_clip(tmp_path):
    now = time.time()
    log = f"{stamp(now - 60)} REMOTE(robot) FALLBACK main=http503 15.2s Robot\n"
    s = voices.state(home(tmp_path, log), now=now, health=lambda url: READY)
    assert s["real"] is False and s["why"] == "the last clip used them; the voices are back now"
    s = voices.state(home(tmp_path, log), now=now + voices.RECENT_S + 1, health=lambda url: READY)
    assert s["real"] is True, "an old fallback is history, not now"


def test_the_old_log_format_still_reads(tmp_path):
    now = time.time()
    log = f"{stamp(now - 60)} REMOTE(robot) FALLBACK http=503 15.2s Robot\n{stamp(now - 30)} REMOTE(robot) 2.0s Robot\n"
    assert voices.state(home(tmp_path, log), now=now, health=lambda url: READY)["real"] is True
    log = f"{stamp(now - 30)} REMOTE(robot) FALLBACK http=503 15.2s Robot\n"
    assert voices.state(home(tmp_path, log), now=now, health=lambda url: READY)["real"] is False


def test_no_voices_in_use_is_never_backup(tmp_path):
    s = voices.state(home(tmp_path, wanted="# nobody yet\n"), health=lambda url: None)
    assert (s["real"], s["why"], s["configured"]) == (True, "", True)


def test_a_cast_file_that_cannot_be_read_is_never_backup(tmp_path):
    (tmp_path / "remote-hosts.conf").write_text(f"main={A}\n")
    (tmp_path / "remote-voices.conf").mkdir()
    assert voices.state(tmp_path, health=lambda url: None)["real"] is True


def test_the_answer_is_cached(tmp_path):
    calls = []
    tts = home(tmp_path)
    voices.state(tts, now=100.0, health=lambda url: calls.append(url))
    voices.state(tts, now=110.0, health=lambda url: calls.append(url))
    assert len(calls) == 1


def test_neither_host_says_why_for_each(tmp_path):
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: IN_USE, B: AWAY}))
    assert s["real"] is False and s["why"] == "main is in use, local is away"


def test_one_host_carrying_everything_is_a_quiet_line(tmp_path):
    s = voices.state(home(tmp_path, hosts=ALL), health=answers({A: IN_USE, B: READY}))
    assert s["real"] is True and s["note"] == "Voices on this PC only: main is in use"


def test_a_later_host_with_some_voices_is_still_backup_for_the_rest(tmp_path):
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: IN_USE, B: READY}))
    assert s["real"] is False and s["why"] == "main is in use; only Narrator on this PC"


def test_the_first_host_alone_carries_everything_when_the_later_is_out(tmp_path):
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: READY, B: AWAY}))
    assert s["real"] is True and s["note"] == "Voices on main only: local is away"


def test_a_host_serving_none_of_the_voices_in_use_is_left_out(tmp_path):
    s = voices.state(home(tmp_path, hosts=f"main={A}\nlocal={B}\n"), health=answers({A: READY, B: AWAY}))
    assert (s["real"], s["why"], s["note"]) == (True, "", "")


def test_both_hosts_are_asked_at_once(tmp_path):
    def slow(url):
        time.sleep(0.5)
        return READY
    started = time.monotonic()
    voices.state(home(tmp_path, hosts=ALL), health=slow)
    assert time.monotonic() - started < 0.9


def test_a_hanging_probe_is_out_of_reach_within_the_deadline(tmp_path):
    def hang(url):
        time.sleep(4)
        return READY
    started = time.monotonic()
    s = voices.state(home(tmp_path, hosts=ALL), health=hang)
    assert time.monotonic() - started < voices.DEADLINE_S + 0.5
    assert s["real"] is False and "off or out of reach" in s["why"]


def test_concurrent_callers_on_a_miss_probe_once_per_host(tmp_path):
    calls = []

    def slow(url):
        calls.append(url)
        time.sleep(0.4)
        return READY
    tts = home(tmp_path, hosts=ALL)
    out = []
    threads = [threading.Thread(target=lambda: out.append(voices.state(tts, health=slow))) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 2 and len(out) == 5 and all(o["real"] for o in out)


def test_a_stale_value_is_served_while_one_refresh_runs(tmp_path):
    tts = home(tmp_path)
    first = voices.state(tts, now=100.0, health=lambda url: READY)
    gate = threading.Event()

    def slow(url):
        gate.wait(2)
        return None
    refresh = threading.Thread(target=lambda: voices.state(tts, now=500.0, health=slow))
    refresh.start()
    time.sleep(0.2)
    started = time.monotonic()
    assert voices.state(tts, now=501.0, health=slow) is first
    assert time.monotonic() - started < 0.3
    gate.set()
    refresh.join()


def test_a_named_host_reads_right_in_every_sentence(tmp_path):
    """The voice_host_names setting: a host on the server's own machine reads
    as "the voice host on this PC", never as its bare name, which can read as
    the dashboard itself being down."""
    off = voices.state(home(tmp_path, hosts=TWO), health=answers({A: READY, B: None}))
    assert off["note"] == "Voices on main only: the voice host on this PC is off or out of reach"
    busy = voices.state(home(tmp_path, hosts=TWO), health=answers({A: READY, B: {"busy": True}}))
    assert busy["note"] == "Voices on main only: this PC's graphics card is busy"
    stood = voices.state(home(tmp_path, hosts=TWO), health=answers({A: READY, B: {"state": "stood_down"}}))
    assert stood["note"] == "Voices on main only: the voice host on this PC stood down"
    down = voices.state(home(tmp_path, hosts=TWO), health=answers({A: None, B: None}))
    assert down["why"] == "main is off or out of reach, the voice host on this PC is off or out of reach"


def test_names_come_only_from_the_setting(tmp_path, monkeypatch):
    assert (voices.subject("local"), voices.possessive("local"), voices.place("local")) == (
        "the voice host on this PC", "this PC's", "this PC")
    assert (voices.subject("main"), voices.possessive("main"), voices.place("main")) == ("main", "main's", "main")
    monkeypatch.setenv("AGENT_COMPANION_CONFIG", str(tmp_path / "none.toml"))
    assert voices.subject("local") == "local"


def test_a_hosts_own_version_still_serves_that_voice(tmp_path):
    # 'narrator+' is the later host's own version of narrator. Still narrator, so the
    # first host standing down leaves it real, and REMOTE(narrator@local) is a real clip.
    now = time.time()
    tts = home(tmp_path, f"{stamp(now - 30)} REMOTE(narrator@local) 8.1s Narrator\n",
               hosts=f"main={A}\nlocal={B} robot,narrator+\n")
    assert voices.hosts(tts)[1][2] == {"robot", "narrator"}
    assert voices.own_versions(tts) == {"narrator": "local"}
    s = voices.state(tts, now=now, health=answers({A: IN_USE, B: READY}))
    assert s["real"] is True and s["note"] == "Voices on this PC only: main is in use"


def test_own_versions_are_read_from_the_hosts_own_row(tmp_path):
    """Comments, blank lines and a bad row before or among the hosts (a
    commented hosts file) must not shift the row a host's own versions come from."""
    tts = home(tmp_path, hosts=f"# voice hosts\nmain={A}\n\n# the later host\nbad=not-a-url narrator+\n"
                               f"local={B} robot,narrator+\n")
    assert [name for name, _url, _voices in voices.hosts(tts)] == ["main", "local"]
    assert voices.own_versions(tts) == {"narrator": "local"}


def test_no_own_versions_without_a_plus_or_a_hosts_file(tmp_path):
    assert voices.own_versions(home(tmp_path, hosts=TWO)) == {}
    assert voices.own_versions(tmp_path / "nowhere") == {}


def test_the_add_on_can_say_why_a_host_is_out(tmp_path, monkeypatch):
    """A reason only the owner knows (a host stopped on purpose) replaces the
    core's words. The add-on is asked only about hosts that are not ready."""
    asked = []

    def status(host, report, now):
        asked.append((host, report))
        return {"why": "this PC's voices are off for now"} if host == "local" else {}
    _plug(monkeypatch, voice_host_status=status)
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: IN_USE, B: None}))
    assert s["why"] == "main is in use, this PC's voices are off for now"
    assert asked == [("main", IN_USE), ("local", None)]


def test_a_resting_host_leaves_the_quiet_line_but_never_makes_it_ready(tmp_path, monkeypatch):
    """Resting is down on purpose (asleep, say): no "Voices on X only" line for
    it, but with every host out the clips still go to the backup voices."""
    _plug(monkeypatch, voice_host_status=lambda host, report, now: {"resting": host == "local"})
    asleep = {"state": "stood_down", "why": "asleep"}
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: READY, B: asleep}))
    assert (s["real"], s["note"]) == (True, "")
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: None, B: asleep}))
    assert s["real"] is False and s["why"] == "main is off or out of reach, asleep"


def test_a_broken_add_on_costs_only_its_words(tmp_path, monkeypatch):
    def broken(host, report, now):
        raise RuntimeError("boom")
    _plug(monkeypatch, voice_host_status=broken)
    s = voices.state(home(tmp_path, hosts=TWO), health=answers({A: READY, B: None}))
    assert s["note"] == "Voices on main only: the voice host on this PC is off or out of reach"
