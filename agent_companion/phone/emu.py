"""The test emulator: Android 15 on agent-companion, headless, for the phone driver.

Spec: docs/superpowers/specs/2026-11-13-phone-driver-design.md, section 4.
Plan: docs/superpowers/plans/2026-11-16-phone-driver-1-emulator.md.

`phone emu ...` (bin/phone) calls these. adb does chores only: the driver
in the agent-companion app drives (step 3). Every adb call names the emulator's
serial, so a phone on wireless debugging is never touched by mistake.

Measured 2026-11-16: a cold boot takes 26 s; a 3 GB guest held 4.4 GB of
anonymous memory at boot, at the 5 GB cap, so the guest is 2 GB. The user
manager has the cpu, memory and pids controllers and no cpuset: the watcher
pins CPUs with sched_setaffinity and lowers CPUWeight, both of which it can
undo. A user process cannot undo nice.

The decisions are pure and take what they read as arguments. The chores go
through `run`, which the tests replace.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from .. import config
from . import job, settings


class _LoadGuard:
    """Optional host load guard; absence means the emulator is unrestricted."""
    STATE = Path("")

    @staticmethod
    def holding() -> str | None:
        return None

    @staticmethod
    def read(_path: Path) -> dict:
        return {}


leases = job.leases
loadguard = _LoadGuard()

SDK = Path(str(config.get("phone_android_sdk") or os.environ.get("ANDROID_HOME")
               or Path.home() / "android-sdk")).expanduser()
PORT = 5580
SERIAL = f"emulator-{PORT}"
UNIT = "phone-emulator"
WATCH_UNIT = "phone-emulator-watch"
GUEST_MB = 2048
MEMORY_MAX = "5G"
SWAP_MAX = "1G"
MIN_FREE_KB = 8 * 1024 * 1024
IDLE_S = 30 * 60
BOOT_S = 180
WATCH_EVERY_S = 15
# An import error kills a new watcher within a few seconds.
WATCH_GRACE_S = 5
DATA = Path(os.environ.get("AGENT_COMPANION_PHONE_DIR")
            or Path.home() / ".local" / "share" / "agent-companion" / "phone")
USED = DATA / "emu-used"
REPO = Path(__file__).resolve().parents[2]
APP_PACKAGE = str(config.get("phone_android_package") or "com.agentcompanion.app")
EMU_SUFFIX = "-emu"
DRIVER_TAG = "AgentCompanionDriver"
LOG_TAGS = ("AgentCompanion:V", f"{DRIVER_TAG}:V", "AndroidRuntime:E", "*:S")
PROMPT_LINE = re.compile(r"PROMPT (?:allow=(\d+),(\d+) deny=(\d+),(\d+)|gone)")
BANNER_LINE = re.compile(
    r"BANNER (?:stop=(?P<stop_x>\d+),(?P<stop_y>\d+)|"
    r"continue=(?P<continue_x>\d+),(?P<continue_y>\d+)|"
    r"resume=(?P<resume_x>\d+),(?P<resume_y>\d+)|offer=none|gone)"
)
NOTICE_LINE = re.compile(r"NOTICE (?:stop=(?P<stop_x>\d+),(?P<stop_y>\d+)|gone)")
CHROME = "com.android.chrome"
CHROME_COMMAND_LINE = "_ --disable-fre --no-default-browser-check --no-first-run"
E2E_PAGE = "http://10.0.2.2:8771/phone-e2e-page.html"


# --- decisions (pure) -------------------------------------------------------------
def kvm_state(exists: bool, access_ok: bool, listed: bool) -> str:
    """'ok': this process opens /dev/kvm. 'sg': user is in the kvm group but
    this login predates it (until WSL restarts), so run under `sg kvm`.
    'none': no KVM for us."""
    if not exists:
        return "none"
    if access_ok:
        return "ok"
    return "sg" if listed else "none"


def mem_available_kb(meminfo: str) -> int:
    m = re.search(r"^MemAvailable:\s+(\d+) kB", meminfo, re.M)
    return int(m.group(1)) if m else 0


def refusal(holding: str | None, mem_kb: int, kvm: str) -> str | None:
    """Why `start` must not start, in words, or None."""
    if holding == "gaming":
        return f"{settings.holder()} is gaming (the load guard says so): the emulator waits until the game ends"
    if holding == "hot":
        return "the CPU is hot (the load guard says so): try again when it cools"
    if mem_kb < MIN_FREE_KB:
        return f"only {mem_kb / 1048576:.1f} GB of memory is free and the emulator needs 8"
    if kvm == "none":
        return "KVM is not usable: /dev/kvm is missing or user is not in the kvm group"
    return None


def emulator_argv(sdk: Path = SDK) -> list[str]:
    return [str(sdk / "emulator" / "emulator"), "-avd", settings.avd(), "-port", str(PORT),
            "-no-window", "-no-audio", "-no-boot-anim", "-gpu", "swiftshader_indirect",
            "-memory", str(GUEST_MB), "-no-metrics",
            # No quick boot: every job resets to the clean snapshot anyway, and
            # a quick-boot state saved by an emulator stopped mid-run made that
            # load die restoring graphics (2026-11-17, FATAL "Failed to load
            # snapshot state for graphics").
            "-no-snapshot-load", "-no-snapshot-save"]


def unit_argv(emu: list[str], kvm: str, sdk: Path = SDK) -> list[str]:
    """A transient user service: it outlives the `phone` command, journald
    keeps its log, and `systemctl --user stop` ends it whole."""
    cmd = ["sg", "kvm", "-c", shlex.join(emu)] if kvm == "sg" else list(emu)
    return ["systemd-run", "--user", f"--unit={UNIT}", "--collect",
            "-p", f"MemoryMax={MEMORY_MAX}", "-p", f"MemorySwapMax={SWAP_MAX}",
            f"--setenv=ANDROID_HOME={sdk}", f"--setenv=ANDROID_SDK_ROOT={sdk}", *cmd]


def watch_argv(python: str = sys.executable, repo: Path = REPO) -> list[str]:
    return ["systemd-run", "--user", f"--unit={WATCH_UNIT}", "--collect",
            python, str(repo / "bin" / "phone"), "emu", "watch"]


def watch_step(holding: str | None, cpus: int, now: float, used_at: float | None,
               lease_live: bool, pinned: bool) -> str:
    """One look by the watcher: stop-lease, stop-idle, pin, unpin or keep."""
    if not lease_live:
        return "stop-lease"
    if used_at is None or now - used_at > IDLE_S:
        return "stop-idle"
    want = holding == "gaming" and cpus > 0
    if want and not pinned:
        return "pin"
    if pinned and not want:
        return "unpin"
    return "keep"


def pinned_set(all_cpus, n: int) -> set[int]:
    """The last n CPUs, the ones `heavy` pins while he games."""
    cpus = sorted(all_cpus)
    return set(cpus[-n:]) if n > 0 else set(cpus)


def badging(text: str) -> tuple[str, str]:
    """(package, versionName) from `aapt dump badging`."""
    m = re.search(r"package: name='([^']+)'.*?versionName='([^']*)'", text)
    return (m.group(1), m.group(2)) if m else ("", "")


def apk_refusal(package: str, version_name: str) -> str | None:
    if not package:
        return "could not read the APK's package name (aapt dump badging)"
    if package == APP_PACKAGE and not version_name.endswith(EMU_SUFFIX):
        return ("this app build is not the emulator build (its version does not end in -emu): "
                "it could register for notifications or comms. Build one with --emulator")
    return None


def prompt_point(logcat: str, which: str) -> tuple[int, int] | None:
    """The held prompt's Allow or Deny centre, from the driver's own log lines
    (`PROMPT allow=x,y deny=x,y` when one goes up, `PROMPT gone` when it is
    answered or lapses). None when no prompt is up."""
    hits = PROMPT_LINE.findall(logcat)
    if not hits or not hits[-1][0]:
        return None
    ax, ay, dx, dy = (int(v) for v in hits[-1])
    return (ax, ay) if which == "allow" else (dx, dy)


def banner_point(logcat: str, which: str) -> tuple[int, int] | None:
    """The driving banner's current Continue, Resume or Stop centre, from its
    log. Continue and Resume disappear at `offer=none`; all disappear at
    `gone`."""
    points: dict[str, tuple[int, int] | None] = {"continue": None, "resume": None, "stop": None}
    for hit in BANNER_LINE.finditer(logcat):
        if hit.group("stop_x"):
            points["stop"] = (int(hit.group("stop_x")), int(hit.group("stop_y")))
        elif hit.group("continue_x"):
            points["continue"] = (int(hit.group("continue_x")), int(hit.group("continue_y")))
        elif hit.group("resume_x"):
            points["resume"] = (int(hit.group("resume_x")), int(hit.group("resume_y")))
        elif hit.group(0).endswith("offer=none"):
            points["continue"] = None
            points["resume"] = None
        else:
            points["continue"] = None
            points["resume"] = None
            points["stop"] = None
    return points.get(which)


def notice_point(logcat: str) -> tuple[int, int] | None:
    """The newest visible notification Stop point in the driver's log."""
    point = None
    for hit in NOTICE_LINE.finditer(logcat):
        if hit.group("stop_x"):
            point = (int(hit.group("stop_x")), int(hit.group("stop_y")))
        else:
            point = None
    return point


def input_arg(text: str) -> str:
    """One argument for `adb shell input text`: spaces as %s (input's own
    escape), single-quoted for the emulator's shell."""
    if not text:
        raise ValueError("nothing to type")
    if "%" in text or any(not (32 <= ord(c) <= 126) for c in text):
        raise ValueError("input types printable ASCII without %, as a demo password needs")
    return "'" + text.replace(" ", "%s").replace("'", "'\\''") + "'"


# --- reading the machine ------------------------------------------------------------
def read_kvm(dev: str = "/dev/kvm", group_file: str = "/etc/group", user: str | None = None) -> str:
    user = user or os.environ.get("USER") or "user"
    listed = False
    try:
        for line in Path(group_file).read_text().splitlines():
            name, _, _, members = (line.split(":") + ["", "", "", ""])[:4]
            if name == "kvm":
                listed = user in members.split(",")
    except OSError:
        pass
    return kvm_state(os.path.exists(dev), os.access(dev, os.R_OK | os.W_OK), listed)


def read_mem_kb() -> int:
    try:
        return mem_available_kb(Path("/proc/meminfo").read_text())
    except OSError:
        return 0


def run(argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    """subprocess.run that answers instead of raising: 124 timed out, 127 not run."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout:.0f} s")
    except OSError as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def adb(runner, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Every adb call names the emulator: never a phone by accident."""
    return runner(["adb", "-s", SERIAL, *args], timeout=timeout)


def _active(unit: str, runner) -> bool:
    out = runner(["systemctl", "--user", "is-active", unit], timeout=10).stdout.strip()
    return out in ("active", "activating")


def unit_active(runner=run) -> bool:
    return _active(UNIT, runner)


def watcher_active(runner=run) -> bool:
    return _active(WATCH_UNIT, runner)


def booted(runner=run) -> bool:
    r = adb(runner, "shell", "getprop", "sys.boot_completed", timeout=10)
    return r.returncode == 0 and r.stdout.strip() == "1"


def touch_used(now: float | None = None) -> None:
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    USED.touch()
    t = time.time() if now is None else now
    os.utime(USED, (t, t))


def used_at() -> float | None:
    try:
        return USED.stat().st_mtime
    except OSError:
        return None


# --- the chores ---------------------------------------------------------------------
NOT_UP = "the emulator is not up: phone emu start"
NO_WATCHER = ("the emulator's watcher would not start, so the emulator is stopped "
              "(it never runs unwatched): journalctl --user -u phone-emulator-watch")


def start(runner=run, clock=time.time, wait=time.sleep) -> tuple[bool, str]:
    if not unit_active(runner):
        kvm = read_kvm()
        why = refusal(loadguard.holding(), read_mem_kb(), kvm)
        if why:
            return False, why
        r = runner(unit_argv(emulator_argv(), kvm), timeout=30)
        if r.returncode != 0:
            return False, "systemd-run failed: " + (r.stderr or r.stdout).strip()[:200]
    # A start is a use, stamped before the watcher's first look: that look
    # comes at once, and on the last session's old stamp it stopped a fresh
    # boot as idle (2026-11-16).
    touch_used()
    if not watcher_active(runner):
        r = runner(watch_argv(), timeout=30)
        wait(WATCH_GRACE_S)
        if r.returncode != 0 or not watcher_active(runner):
            stop(runner, wait=wait)
            return False, NO_WATCHER
    deadline = clock() + BOOT_S
    while clock() < deadline:
        if booted(runner):
            if not watcher_active(runner):
                stop(runner, wait=wait)
                return False, NO_WATCHER
            return True, f"the emulator is up ({SERIAL})"
        if not unit_active(runner):
            return False, "the emulator exited while booting: journalctl --user -u phone-emulator"
        wait(2)
    stop(runner, wait=wait)
    return False, f"the emulator did not finish booting in {BOOT_S} s, so it is stopped"


def stop(runner=run, wait=time.sleep, watcher: bool = True) -> tuple[bool, str]:
    """Gracefully (adb emu kill saves the quick-boot state), then by systemd."""
    said = "the emulator was not running"
    if unit_active(runner):
        adb(runner, "emu", "kill", timeout=30)
        for _ in range(30):
            if not unit_active(runner):
                break
            wait(1)
        if unit_active(runner):
            runner(["systemctl", "--user", "stop", UNIT], timeout=60)
        if unit_active(runner):
            return False, "the emulator would not stop: systemctl --user status phone-emulator"
        said = "the emulator is stopped"
    if watcher:
        runner(["systemctl", "--user", "stop", WATCH_UNIT], timeout=30)
    return True, said


def aapt_path(sdk: Path = SDK) -> Path | None:
    found = sorted((sdk / "build-tools").glob("*/aapt"))
    return found[-1] if found else None


def install(apk: str, runner=run) -> tuple[bool, str]:
    p = Path(apk).expanduser()
    if not p.is_file():
        return False, f"no such APK: {apk}"
    tool = aapt_path()
    if tool is None:
        return False, "aapt is missing from ~/android-sdk/build-tools"
    pkg, ver = badging(runner([str(tool), "dump", "badging", str(p)], timeout=60).stdout)
    why = apk_refusal(pkg, ver)
    if why:
        return False, why
    if not booted(runner):
        return False, NOT_UP
    r = adb(runner, "install", "-r", "-t", str(p), timeout=300)
    touch_used()
    out = (r.stdout + r.stderr).strip()
    if r.returncode == 0 and "Success" in out:
        if pkg == APP_PACKAGE:
            granted = adb(runner, "shell",
                          f"pm grant {APP_PACKAGE} android.permission.POST_NOTIFICATIONS", timeout=30)
            if granted.returncode != 0:
                detail = (granted.stderr or granted.stdout or "pm grant failed").strip()
                return False, f"installed {pkg} {ver}, but could not grant its notification permission: {detail}"
            return True, f"installed {pkg} {ver}; granted its notification permission on the emulator"
        return True, f"installed {pkg} {ver}"
    return False, "install failed: " + out[-300:]


def _snapshot(runner, verb: str, said: str, wait=time.sleep) -> tuple[bool, str]:
    if not booted(runner):
        return False, NOT_UP
    r = adb(runner, "emu", "avd", "snapshot", verb, "clean", timeout=300)
    touch_used()
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "KO" in out:
        hint = " (make it once with phone emu save-clean)" if verb == "load" else ""
        return False, f"snapshot {verb} failed: {out[:200]}{hint}"
    if verb == "load":
        # adb loses the emulator for a few seconds after a load (measured
        # 2026-11-16): say it is back only once it answers as booted again.
        for _ in range(30):
            if booted(runner):
                break
            wait(2)
        else:
            return False, "the clean snapshot loaded but the emulator did not answer again within 60 s"
    return True, said


def reset(runner=run, wait=time.sleep) -> tuple[bool, str]:
    return _snapshot(runner, "load", "back to the clean snapshot", wait)


def save_clean(runner=run) -> tuple[bool, str]:
    return _snapshot(runner, "save", "saved this state as the clean snapshot")


def logs(lines: int = 300, runner=run) -> tuple[bool, str]:
    if not booted(runner):
        return False, NOT_UP
    r = adb(runner, "logcat", "-d", "-v", "time", "-t", str(int(lines)), *LOG_TAGS, timeout=30)
    return r.returncode == 0, (r.stdout or r.stderr).rstrip()


def answer(which: str, runner=run, clock=time.monotonic, wait=time.sleep) -> tuple[bool, str]:
    """A touch on a held prompt or the driving banner, as the device holder's finger would."""
    if which not in ("allow", "deny", "continue", "stop", "resume", "notice-stop"):
        return False, "answer allow, deny, continue, stop, resume or notice-stop"
    if not booted(runner):
        return False, NOT_UP
    if which == "notice-stop":
        # The shade lookup must not reuse a point recorded before expanding it:
        # a notification can have gone away since then. A log dump appends, so
        # an exact prefix gives a safe boundary; if it changed otherwise, refuse.
        before = adb(runner, "logcat", "-d", "-s", f"{DRIVER_TAG}:I", timeout=30).stdout
        expanded = adb(runner, "shell", "cmd statusbar expand-notifications", timeout=30)
        if expanded.returncode != 0:
            return False, "could not open the notification shade: " + (expanded.stderr or expanded.stdout).strip()
        deadline = clock() + 5
        tapped = None
        try:
            while True:
                after = adb(runner, "logcat", "-d", "-s", f"{DRIVER_TAG}:I", timeout=30).stdout
                fresh = after[len(before):] if after.startswith(before) else ""
                point = notice_point(fresh)
                if point is not None:
                    tapped = adb(runner, "shell", f"input tap {point[0]} {point[1]}", timeout=30)
                    touch_used()
                    break
                remaining = deadline - clock()
                if remaining <= 0:
                    break
                wait(min(1, remaining))
        finally:
            adb(runner, "shell", "cmd statusbar collapse", timeout=30)
        if tapped is None:
            return False, "no notification Stop appeared on the emulator"
        if tapped.returncode != 0:
            return False, "could not touch the notification Stop: " + (tapped.stderr or tapped.stdout).strip()
        return True, "touched the notification Stop"
    logcat = adb(runner, "logcat", "-d", "-s", f"{DRIVER_TAG}:I", timeout=30).stdout
    point = prompt_point(logcat, which) if which in ("allow", "deny") else banner_point(logcat, which)
    if point is None:
        if which == "continue":
            return False, "the banner has no Continue offer on the emulator"
        if which == "resume":
            return False, "the banner has no Resume offer on the emulator"
        if which == "stop":
            return False, "no driving banner is up on the emulator"
        return False, "no prompt is up on the emulator"
    r = adb(runner, "shell", "input", "tap", str(point[0]), str(point[1]), timeout=30)
    touch_used()
    return r.returncode == 0, f"touched {which.capitalize()} at {point[0]},{point[1]}"


def user(what: str, arg: str = "", runner=run) -> tuple[bool, str]:
    """the device holder's measured hardware, lock-screen, call and touchscreen actions."""
    shell = {
        "power": ["input keyevent KEYCODE_SLEEP"],
        "wake": ["input keyevent KEYCODE_WAKEUP"],
        "home": ["input keyevent KEYCODE_HOME"],
        "lock": ["locksettings set-disabled false", "input keyevent KEYCODE_SLEEP",
                 "input keyevent KEYCODE_WAKEUP"],
        "unlock": ["wm dismiss-keyguard"],
        "lock-off": ["locksettings set-disabled true"],
    }
    said = {
        "power": "pressed the power button",
        "wake": "woke the emulator",
        "home": "pressed Home",
        "lock": "locked the emulator",
        "unlock": "unlocked the emulator",
        "lock-off": "turned the emulator lock screen off",
        "call": "called the emulator",
        "accept": "accepted the emulator call",
        "hangup": "hung up the emulator call",
        "touch": "touched the emulator",
    }
    if what not in (*shell, "call", "accept", "hangup", "touch"):
        return False, "user power, wake, home, lock, unlock, lock-off, call, accept, hangup or touch X,Y"
    point = None
    if what == "touch":
        m = re.fullmatch(r"(\d+),(\d+)", str(arg))
        if m is None or any(int(v) > 4000 for v in m.groups()):
            return False, "user touch takes X,Y, with each point from 0 to 4000"
        point = (int(m.group(1)), int(m.group(2)))
    if not booted(runner):
        return False, NOT_UP
    if what in shell:
        for command in shell[what]:
            result = adb(runner, "shell", command, timeout=30)
            if result.returncode != 0:
                break
    elif what in ("call", "accept", "hangup"):
        verb = {"call": "call", "accept": "accept", "hangup": "cancel"}[what]
        result = adb(runner, "emu", "gsm", verb, "5551234", timeout=30)
    else:
        result = adb(runner, "shell", f"input tap {point[0]} {point[1]}", timeout=30)
    touch_used()
    if result.returncode == 0:
        return True, said[what]
    return False, f"could not {said[what]}: " + (result.stderr or result.stdout or "adb failed").strip()


def type_text(text: str, runner=run) -> tuple[bool, str]:
    """Typing into the focused field, as the device holder would (a demo password)."""
    try:
        arg = input_arg(text)
    except ValueError as e:
        return False, str(e)
    if not booted(runner):
        return False, NOT_UP
    r = adb(runner, "shell", "input", "text", arg, timeout=30)
    touch_used()
    if r.returncode == 0:
        return True, "typed it into the focused field"
    return False, (r.stderr or "input failed").strip()


def chrome_page(runner=run) -> tuple[bool, str]:
    """Start Chrome on the fixed local gate-test page with first run skipped."""
    if not booted(runner):
        return False, NOT_UP
    # adb joins a shell command's arguments with spaces and the emulator's own
    # shell parses that line again (type_text quotes its text for the same
    # reason), so the whole command goes as one string: a `sh -c` script would
    # keep only its first word.
    wrote = adb(runner, "shell", f"echo '{CHROME_COMMAND_LINE}' > /data/local/tmp/chrome-command-line", timeout=30)
    if wrote.returncode != 0:
        return False, "could not set Chrome's command line: " + (wrote.stderr or wrote.stdout).strip()
    adb(runner, "shell", "am", "force-stop", CHROME, timeout=30)
    opened = adb(runner, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", E2E_PAGE,
                 CHROME, timeout=30)
    touch_used()
    if opened.returncode == 0:
        return True, "opened the phone end-to-end page in Chrome"
    return False, "could not open the phone end-to-end page in Chrome: " + (opened.stderr or opened.stdout).strip()


def messages_draft(runner=run) -> tuple[bool, str]:
    """Open the fixed Messages draft that exercises the Send gate."""
    if not booted(runner):
        return False, NOT_UP
    r = adb(runner, "shell", "am", "start", "-a", "android.intent.action.SENDTO", "-d", "sms:5551234",
            "--es", "sms_body", "'On my way'", timeout=30)     # quoted for the emulator's shell (chrome_page)
    touch_used()
    if r.returncode == 0:
        return True, "opened the Messages test draft"
    return False, "could not open the Messages test draft: " + (r.stderr or r.stdout).strip()


def messages_code(runner=run) -> tuple[bool, str]:
    """Send the fixed code SMS, then open the thread without reading it."""
    if not booted(runner):
        return False, NOT_UP
    sent = adb(runner, "emu", "sms", "send", "5551234",
               "G-482913 is your Google verification code.", timeout=30)
    touch_used()
    if sent.returncode != 0:
        return False, "could not send the Messages verification code: " + (sent.stderr or sent.stdout).strip()
    # adb joins shell arguments and Android parses them again. Keep this whole
    # command in one string, as chrome_page does.
    opened = adb(runner, "shell", "am start -a android.intent.action.SENDTO -d sms:5551234", timeout=30)
    if opened.returncode == 0:
        return True, "sent the verification code and opened its Messages thread"
    return False, "could not open the Messages code thread: " + (opened.stderr or opened.stdout).strip()


def status(runner=run) -> dict:
    lease = leases.live("emulator")
    up = unit_active(runner)
    return {"running": up, "booted": booted(runner) if up else False, "serial": SERIAL,
            "used_at": used_at(),
            "lease": {k: lease.get(k) for k in ("holder", "why", "until")} if lease else None}


# --- the watcher ---------------------------------------------------------------------
def unit_pids(runner=run) -> list[int]:
    cg = runner(["systemctl", "--user", "show", UNIT, "-p", "ControlGroup", "--value"], timeout=10).stdout.strip()
    if not cg.startswith("/") or UNIT not in cg:
        return []          # never the root cgroup: that would pin every process
    try:
        return [int(p) for p in Path("/sys/fs/cgroup" + cg, "cgroup.procs").read_text().split() if p.isdigit()]
    except OSError:
        return []


def set_affinity(cpus: set[int], runner=run) -> int:
    done = 0
    for pid in unit_pids(runner):
        try:
            tids = [int(t) for t in os.listdir(f"/proc/{pid}/task")]
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(tid, cpus)
                done += 1
            except OSError:
                pass
    return done


def pin(n: int, runner=run) -> None:
    set_affinity(pinned_set(os.sched_getaffinity(0), n), runner)
    runner(["systemctl", "--user", "set-property", "--runtime", UNIT, "CPUWeight=1"], timeout=10)


def unpin(runner=run) -> None:
    set_affinity(set(os.sched_getaffinity(0)), runner)
    runner(["systemctl", "--user", "set-property", "--runtime", UNIT, "CPUWeight=100"], timeout=10)


def watch(runner=run, clock=time.time, wait=time.sleep, once: bool = False) -> str:
    """The watcher (its own transient unit, started by `start`): ends when the
    emulator does, and stops it after IDLE_S unused or when its lease ends."""
    pinned = False
    while True:
        if not unit_active(runner):
            return "gone"
        guard = loadguard.read(loadguard.STATE) or {}
        cpus = int(guard.get("cpus") or 0)
        step = watch_step(loadguard.holding(), cpus, clock(), used_at(),
                          leases.live("emulator") is not None, pinned)
        if step.startswith("stop"):
            stop(runner, wait=wait, watcher=False)
            return step
        if step == "pin":
            pin(cpus, runner)
            pinned = True
        elif step == "unpin":
            unpin(runner)
            pinned = False
        if once:
            return step
        wait(WATCH_EVERY_S)
