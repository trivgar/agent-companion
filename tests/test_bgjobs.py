"""Background shell jobs of a Claude session, from a fixture /proc, and the
redacted one-line label each job gets on its card."""
import shlex

from agent_companion import bgjobs


def _proc(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("btime 1000\n")

    def add(pid, ppid, started, command, comm="process"):
        folder = proc / str(pid)
        folder.mkdir()
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(started)]
        (folder / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
        (folder / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")

    return proc, add


def _shell(command):
    script = ("source /tmp/.claude/shell-snapshots/snapshot-bash-7.sh && eval "
              + shlex.quote(command))
    return ["/bin/bash", "-c", script]


def test_scan_lists_direct_snapshot_shells_with_command_and_age(tmp_path, monkeypatch):
    proc, add = _proc(tmp_path)
    add(201, 101, 200, _shell("pytest -q tests"), "bash worker")
    add(202, 101, 300, ["/bin/bash", "-c", "echo foreground"], "bash")
    add(203, 999, 100, _shell("sleep 60"), "bash")
    monkeypatch.setattr(bgjobs, "_clock_ticks", lambda: 100)

    assert bgjobs.scan({101}, str(proc), now=1105) == {
        101: [{"cmd": "pytest -q tests", "age_s": 103}],
    }


def test_only_snapshot_shells_are_jobs_and_a_job_is_counted_once(tmp_path, monkeypatch):
    proc, add = _proc(tmp_path)
    add(101, 1, 1, ["claude", "--resume", "x"], "claude")
    add(102, 101, 1, ["python3", "/home/me/.claude/tts/mcp/server.py"], "python3")
    add(103, 101, 1, ["node", "/x/pyright-langserver", "--stdio"], "node")
    add(104, 101, 1, ["-bash"], "bash")                       # a login shell is no job
    add(110, 101, 2000, _shell("pytest -q tests/"), "bash")
    add(111, 110, 2000, ["timeout", "590", "pytest"], "timeout")
    add(112, 111, 2000, ["pytest"], "pytest")
    add(113, 112, 2000, _shell("sleep 5"), "bash")             # a shell under a job is that job
    add(120, 101, 1000, _shell("npm run build"), "bash")
    monkeypatch.setattr(bgjobs, "_clock_ticks", lambda: 100)

    jobs = bgjobs.scan({101}, str(proc), now=1100)[101]

    assert [job["cmd"] for job in jobs] == ["npm run build", "pytest -q tests/"]
    assert [job["age_s"] for job in jobs] == [90, 80]


def test_a_comm_with_spaces_and_parentheses_does_not_shift_the_fields(tmp_path, monkeypatch):
    proc, add = _proc(tmp_path)
    add(110, 101, 0, _shell("make all"), "bash")
    fields = ["S", "101"] + ["0"] * 17 + ["1500"]
    (proc / "110" / "stat").write_text("110 (ba) sh (x)) " + " ".join(fields) + "\n")
    monkeypatch.setattr(bgjobs, "_clock_ticks", lambda: 100)

    assert bgjobs.scan({101}, str(proc), now=1100) == {101: [{"cmd": "make all", "age_s": 85}]}


def test_a_missing_proc_or_a_bad_pid_is_no_jobs(tmp_path):
    assert bgjobs.scan({101}, str(tmp_path / "nope")) == {101: []}
    assert bgjobs.jobs_for("junk") == []


def test_command_labels_redact_and_bound_sensitive_commands():
    command = _shell("curl --header=Authorization: Bearer secret-value")

    label = bgjobs.command_of(command, limit=30)

    assert "secret-value" not in label
    assert "[redacted]" in label
    assert len(label) <= 30


def test_a_label_is_sixty_characters_with_an_ellipsis_where_it_was_cut():
    label = bgjobs.command_of(_shell("echo " + "x" * 200))
    assert len(label) == 60 and label.endswith("…")
    assert bgjobs.command_of(_shell("pytest -q tests")) == "pytest -q tests"


def test_annotate_scans_all_public_rows_once(tmp_path, monkeypatch):
    proc, add = _proc(tmp_path)
    add(201, 101, 200, _shell("pytest -q"), "bash")
    monkeypatch.setattr(bgjobs, "_clock_ticks", lambda: 100)
    rows = [{"pid": 101}, {"pid": 102}, {"pid": 101, "private": True}]

    bgjobs.annotate(rows, str(proc), now=1105)

    assert rows[0]["bg_jobs"] == 1
    assert rows[0]["bg_job_list"][0]["cmd"] == "pytest -q"
    assert rows[1] == {"pid": 102, "bg_jobs": 0, "bg_job_list": []}
    assert rows[2]["bg_jobs"] == 0 and rows[2]["bg_job_list"] == []


def test_redact_hides_underscored_names_flags_bearer_and_url_userinfo():
    cases = {
        "GH_TOKEN=abc123 gh pr list": "abc123",
        "DB_PASSWORD=hunter2 ./run": "hunter2",
        "mytool --api-token s3cret": "s3cret",
        "mysql --password=pw1 db": "pw1",
        "curl -u admin:pw2 https://x.test": "pw2",
        "curl -H 'Authorization: Bearer zzz' https://x.test": "zzz",
        "git clone https://user:pw3@example.com/r.git": "pw3",
        "git clone https://u:a@b@example.com/r.git": "a@b",
        "curl -uadmin:pw4 https://x.test": "pw4",
        "mysql -ppw5 db": "pw5",
        "tool --pass pw6": "pw6",
        "curl --proxy-user u:pw7 https://x.test": "pw7",
        "curl -su u:pw8 https://x.test": "pw8",
        "curl https://x.test/?a=1&access_token=pw9": "pw9",
        "curl https://x.test/?x=1&password=pw10": "pw10",
        "run a=1,password=pw11": "pw11",
        "curl -U a:pw12 https://x.test": "pw12",
        "curl --header 'X: pw13' https://x.test": "pw13",
        "smbclient -U user%pw14 //h/s": "pw14",
    }
    for command, secret in cases.items():
        assert secret not in bgjobs.redact(command), command
    assert bgjobs.redact("pytest -q tests") == "pytest -q tests"


def test_redact_hides_a_value_wherever_one_is_named():
    cases = {
        "API_TOKEN=s3cr3t run": "s3cr3t",
        "run --password hunter2": "hunter2",
        "go key=zzz9 now": "zzz9",
        "tool --token qqq8": "qqq8",
        "sshpass -p pw15 ssh me@host": "pw15",
        "htpasswd -b users.db me pw16": "pw16",
        "/usr/bin/htpasswd -b users.db me pw17": "pw17",
        "openssl enc -passin pass:pw18 -in x": "pw18",
        "curl -d '{\"password\":\"pw19\"}' https://x.test": "pw19",
        "curl -b sessionid=pw20 https://x.test": "pw20",
        "curl 'https://x.test/f?X-Amz-Signature=pw21'": "pw21",
        "mysql -u root -ppw22 db": "pw22",
        "curl --user=admin:pw23 https://x.test": "pw23",
        "curl --user admin:pw24 https://x.test": "pw24",
        "java -Dapi.key=pw25 -jar app.jar": "pw25",
    }
    for command, secret in cases.items():
        assert secret not in bgjobs.redact(command), command


def test_an_everyday_command_reads_whole():
    # A path's folders are names, never values; -u, -p and --user are judged
    # by what they carry; --no-<x> takes no value.
    for command in [
        "cd /mnt/c/Users/me/session-companion && python3 -m pytest -q",
        "python3 -m pytest -q -p no:cacheprovider tests/test_x.py > "
        "/tmp/claude-1000/-mnt-c-Users-me-session-companion/abc/scratchpad/run.log 2>&1",
        "cat docs/specs/2026-11-01-one-back-end-design.md",
        "tmux new-session -d -s build",
        'mkdir -p /tmp/x && mkdir -p "$OUT"',
        "ps -L -u me --no-headers | wc -l",
        "sort -u names.txt",
        "python3 -u serve.py",
        "pip install -U pip",
        "env -u DISPLAY ./run",
        "ssh -p 2222 me@host uptime",
        "xargs -P 4 -n 1 echo",
        "docker run -p 8080:80 img",
        "ls ~/bypass/notes",
        "until ! pgrep -f mut.py; do sleep 5; done",
        "systemctl --user restart app",
        "curl -s https://api.example.com/user/repos",
        "git log origin/main..HEAD",
    ]:
        assert bgjobs.redact(command) == command, command


def test_redact_is_bounded_on_a_huge_command():
    assert len(bgjobs.redact("a" * 100000)) <= bgjobs.SCAN_LIMIT
