"""The Android build flags for the isolated emulator phone driver package."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "mobile/android/app"

GRADLEW = r"""#!/usr/bin/env bash
set -eu
printf '%s\n' "$@" > "$FAKE_LOG/gradlew.args"
mkdir -p app/build/outputs/apk/debug
printf 'apk\n' > app/build/outputs/apk/debug/app-debug.apk
"""


def _exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def tree(tmp_path):
    mobile, log = tmp_path / "mobile", tmp_path / "log"
    log.mkdir()
    mobile.mkdir()
    shutil.copy(ROOT / "mobile/build.sh", mobile / "build.sh")
    (mobile / "configure.py").parent.mkdir(parents=True, exist_ok=True)
    (mobile / "configure.py").write_text("print('configured')\n")
    _exe(mobile / "android/gradlew", GRADLEW)
    _exe(tmp_path / "bin/npx", "#!/usr/bin/env bash\nexit 0\n")
    env = os.environ.copy()
    env.update(PATH=f"{tmp_path / 'bin'}:{env['PATH']}", FAKE_LOG=str(log))
    return {"mobile": mobile, "log": log, "env": env}


def _build(tree, *args):
    return subprocess.run(["bash", str(tree["mobile"] / "build.sh"), *args],
                          env=tree["env"], capture_output=True, text=True, timeout=30)


def _gradle_args(tree):
    return (tree["log"] / "gradlew.args").read_text().splitlines()


def test_default_build_does_not_enable_the_driver(tree):
    result = _build(tree)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _gradle_args(tree) == ["assembleDebug"]


def test_driver_flag_enables_only_the_driver(tree):
    result = _build(tree, "--driver")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _gradle_args(tree) == ["assembleDebug", "-PdriverEnabled=true"]
    assert not (tree["mobile"] / "android/app/build/outputs/apk/debug/app-debug-emulator.apk").exists()


def test_emulator_build_is_a_separate_opted_in_apk(tree):
    result = _build(tree, "--emulator")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _gradle_args(tree) == ["assembleDebug", "-PemulatorBuild=true", "-PdriverEnabled=true"]
    apk = tree["mobile"] / "android/app/build/outputs/apk/debug/app-debug-emulator.apk"
    assert apk.read_text() == "apk\n"
    assert stat.S_IMODE(apk.stat().st_mode) == 0o600
    assert "emulator driver APK" in result.stdout


@pytest.mark.parametrize("args", [("--unknown",), ("--driver", "--bad")])
def test_unrecognized_build_flags_do_not_configure_or_build(tree, args):
    result = _build(tree, *args)
    assert result.returncode == 2
    assert "usage: build.sh [--emulator] [--driver]" in result.stderr
    assert not (tree["log"] / "gradlew.args").exists()


def test_gradle_defaults_the_service_off_and_marks_emulator_builds():
    gradle = (APP / "build.gradle").read_text()
    assert 'def emulatorBuild = (project.findProperty("emulatorBuild") ?: "false").toString() == "true"' in gradle
    assert 'def driverEnabled = emulatorBuild || (project.findProperty("driverEnabled") ?: "false").toString() == "true"' in gradle
    assert 'buildConfigField "boolean", "EMULATOR_BUILD", emulatorBuild ? "true" : "false"' in gradle
    assert 'resValue "bool", "driver_enabled", driverEnabled ? "true" : "false"' in gradle
