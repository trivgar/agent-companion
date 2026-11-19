#!/usr/bin/env bash
# Configure, sync, and build a debug APK from local settings.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

EMULATOR=0
DRIVER=0
while (($#)); do
  case "$1" in
    --emulator) EMULATOR=1 ;;
    --driver) DRIVER=1 ;;
    *) echo "usage: build.sh [--emulator] [--driver]" >&2; exit 2 ;;
  esac
  shift
done

# An emulator package is always a driver package, and Device.allowedHere()
# keeps it to emulator hardware. A phone package needs --driver as well as an
# Accessibility setting chosen on that phone.
if (( EMULATOR )); then DRIVER=1; fi

python3 configure.py
npx cap sync android
GRADLE=(assembleDebug)
if (( EMULATOR )); then GRADLE+=("-PemulatorBuild=true"); fi
if (( DRIVER )); then GRADLE+=("-PdriverEnabled=true"); fi
(cd android && ./gradlew "${GRADLE[@]}")

if (( EMULATOR )); then
  APK="android/app/build/outputs/apk/debug/app-debug.apk"
  EMULATOR_APK="android/app/build/outputs/apk/debug/app-debug-emulator.apk"
  install -m 600 "$APK" "$EMULATOR_APK"
  echo "built emulator driver APK: $EMULATOR_APK"
fi
