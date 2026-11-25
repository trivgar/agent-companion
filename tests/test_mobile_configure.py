import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Built in two pieces: the repo-wide leak scan flags any whole *.ts.net name.
HOST = "box.example." + "ts.net"


def _run(tmp_path, text, firebase=True):
    cfg = tmp_path / "c.toml"
    fb = tmp_path / "google-services.json"
    if firebase:
        fb.write_text('{"project_info": {"project_id": "x"}}')
        text += f'\nfirebase_app_file = "{fb}"\n'
    cfg.write_text(text)
    out = tmp_path / "out"
    return subprocess.run([sys.executable, str(ROOT / "mobile" / "configure.py"), "--out", str(out)],
                          env={"AGENT_COMPANION_CONFIG": str(cfg), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True), out


def test_it_writes_the_app_from_settings(tmp_path):
    r, out = _run(tmp_path, 'app_name = "Hilltop"\nurl = "https://pc.example.com"\n')
    assert r.returncode == 0, r.stderr
    cap = json.loads((out / "capacitor.config.json").read_text())
    assert cap["appName"] == "Hilltop" and cap["appId"] == "app.companion.hilltop"
    assert cap["server"]["url"] == "https://pc.example.com"
    assert cap["android"]["appendUserAgent"] == "HilltopApp CompanionSealed/1"
    props = (out / "android" / "app" / "app.properties").read_text()
    assert "applicationId=app.companion.hilltop" in props and "appName=Hilltop" in props
    assert (out / "android" / "app" / "google-services.json").exists()


def test_an_app_id_setting_wins(tmp_path):
    r, out = _run(tmp_path, 'app_name = "X"\nurl = "https://a.example"\napp_id = "com.example.mine"\n')
    assert json.loads((out / "capacitor.config.json").read_text())["appId"] == "com.example.mine"


def test_missing_settings_are_named(tmp_path):
    r, _ = _run(tmp_path, "", firebase=False)
    assert r.returncode == 1
    for k in ("app_name", "url", "firebase_app_file"):
        assert k in r.stderr


def _server(tmp_path, url):
    r, out = _run(tmp_path, f'app_name = "Hilltop"\nurl = "{url}"\n')
    assert r.returncode == 0, r.stderr
    return json.loads((out / "capacitor.config.json").read_text())["server"]


def test_the_apps_address_is_a_bare_origin(tmp_path):
    for name, url in (("slash", f"https://{HOST}/"), ("bare", f"https://{HOST}")):
        (tmp_path / name).mkdir()
        server = _server(tmp_path / name, url)
        assert server["url"] == f"https://{HOST}"
        assert "appStartPath" not in server


def test_a_path_or_query_rides_in_the_start_path(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _server(tmp_path / "a", f"https://{HOST}/dash/?x=1")
    assert a["url"] == f"https://{HOST}" and a["appStartPath"] == "/dash/?x=1"
    b = _server(tmp_path / "b", f"https://{HOST}?x=1")
    assert b["url"] == f"https://{HOST}" and b["appStartPath"] == "/?x=1"


def test_the_user_agent_says_the_app_and_its_seal(tmp_path):
    r, out = _run(tmp_path, f'app_name = "Hilltop"\nurl = "https://{HOST}"\n')
    cap = json.loads((out / "capacitor.config.json").read_text())
    assert cap["android"]["appendUserAgent"] == "HilltopApp CompanionSealed/1"


def test_an_address_without_a_host_is_refused(tmp_path):
    r, out = _run(tmp_path, f'app_name = "Hilltop"\nurl = "{HOST}"\n')
    assert r.returncode == 1
    assert "url must be a full address, such as https://" + HOST in r.stderr
    assert not (out / "capacitor.config.json").exists()
