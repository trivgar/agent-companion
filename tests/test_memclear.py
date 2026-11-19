"""``agent_companion.memclear`` against a fixture procfs.

Nothing here signals a real process: kill, sleep, clock and reap are fakes.
"""
import signal

from agent_companion import memclear as mc

GB = 1024 ** 3
TMUX = "0::/work.slice/app-tmux.slice/tmux-spawn-1.scope"
SERVICE = "0::/work.slice/voice-host.service"
DAEMON = "java -cp x org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.14.3"
KOTLIN = "java -cp y org.jetbrains.kotlin.daemon.KotlinCompileDaemon --daemon"


class Proc:
    """A procfs whose CPU counters move between the two looks for busy pids."""

    def __init__(self, tmp_path, free_gb):
        self.root = tmp_path / "proc"
        self.root.mkdir()
        self.set_free(free_gb)
        (self.root / "uptime").write_text("100000.0 1.0\n")
        self.rows = {}

    def set_free(self, gb):
        (self.root / "meminfo").write_text(f"MemTotal: 20000000 kB\nMemAvailable: {int(gb * 1024 * 1024)} kB\n")

    def add(self, pid, cmd, rss_gb, age_s=3600, cgroup=TMUX, busy=False):
        self.rows[pid] = dict(cmd=cmd, rss=int(rss_gb * GB / 4096), start=int((100000 - age_s) * 100),
                              cpu=1000, busy=busy, cgroup=cgroup)
        self._write(pid)

    def _write(self, pid):
        row = self.rows[pid]
        directory = self.root / str(pid)
        directory.mkdir(exist_ok=True)
        fields = ["S", "1"] + ["0"] * 9 + [str(row["cpu"]), "0"] + ["0"] * 6 + [str(row["start"]), "0", str(row["rss"])]
        (directory / "stat").write_text(f"{pid} (x y) " + " ".join(fields) + "\n")
        (directory / "cmdline").write_bytes(row["cmd"].replace(" ", "\0").encode() + b"\0")
        (directory / "cgroup").write_text(row["cgroup"] + "\n")

    def tick(self, _seconds=None):
        for pid, row in self.rows.items():
            if row["busy"]:
                row["cpu"] += 500
                self._write(pid)


def test_room_enough_does_nothing(tmp_path):
    proc = Proc(tmp_path, 20)
    proc.add(10, DAEMON, 2)
    sent = []
    note = mc.preflight(10 * GB, str(proc.root), sleep=proc.tick, kill=lambda *args: sent.append(args), reap=lambda: 0)
    assert note == {"need_gb": 10.0, "free_gb": 20.0, "ok": True} and sent == []


def test_short_stops_idle_daemons_names_idle_hogs_and_waits(tmp_path):
    proc = Proc(tmp_path, 4)
    proc.add(10, DAEMON, 2.2)
    proc.add(11, KOTLIN, 1.1)
    proc.add(12, DAEMON, 2.0, busy=True)                 # building: left alone
    proc.add(20, "node server.js", 3.0)                  # idle hog in a tmux session: named
    proc.add(21, "python voice_server.py", 2.0, cgroup=SERVICE)   # a service: never named
    proc.add(22, "node fresh.js", 3.0, age_s=60)         # too young
    sent, elapsed = [], [0.0]

    def sleep(seconds):
        proc.tick()
        elapsed[0] += seconds
    note = mc.preflight(10 * GB, str(proc.root), sleep=sleep, clock=lambda: elapsed[0],
                        kill=lambda pid, sig: sent.append((pid, sig)), reap=lambda: 2, wait_s=60, poll_s=30)
    assert sent == [(10, signal.SIGTERM), (11, signal.SIGTERM)]
    assert [label.split()[0] for label in note["stopped"]] == ["GradleDaemon", "KotlinCompileDaemon"]
    assert note["reaped"] == 2
    assert note["hogs"] == ["node 3.0 GB, idle, 60 min old (pid 20)"]
    assert note["ok"] is False and note["waited_s"] >= 60


def test_a_running_gradle_client_keeps_every_daemon(tmp_path):
    proc = Proc(tmp_path, 4)
    proc.add(10, DAEMON, 2.2)
    proc.add(30, "java -cp w org.gradle.wrapper.GradleWrapperMain build", 0.1, busy=True)
    sent, elapsed = [], [0.0]
    note = mc.preflight(10 * GB, str(proc.root), sleep=lambda seconds: (proc.tick(), elapsed.__setitem__(0, elapsed[0] + seconds)),
                        clock=lambda: elapsed[0], kill=lambda *args: sent.append(args), reap=lambda: 0, wait_s=0)
    assert sent == [] and note["stopped"] == []


def test_room_freed_while_waiting_ends_the_wait(tmp_path):
    proc = Proc(tmp_path, 4)
    elapsed = [0.0]

    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= 30:
            proc.set_free(12)
    note = mc.preflight(10 * GB, str(proc.root), sleep=sleep, clock=lambda: elapsed[0], kill=lambda *args: None,
                        reap=lambda: 0, wait_s=600, poll_s=30)
    assert note["ok"] is True and note["free_gb"] == 12.0 and note["waited_s"] <= 60


def test_a_broken_proc_still_lets_the_suite_run(tmp_path):
    note = mc.preflight(10 * GB, str(tmp_path / "nope"), sleep=lambda seconds: None, reap=lambda: 0)
    assert note["ok"] is True          # nothing known: no stop, no wait
