#!/bin/sh
# Install the APK on a running emulator, open it, and wait for the app's server to answer.
set -e
adb install -r "$1"
adb shell am start -W -n com.andreasthinks.crossposter/.MainActivity
adb forward tcp:47616 tcp:47616
for i in $(seq 1 90); do
  if curl -sf http://127.0.0.1:47616/healthz; then
    echo; echo "app server is up after ${i}x2s"
    adb exec-out screencap -p > android-screenshot.png || true
    exit 0
  fi
  sleep 2
done
echo "app server never answered"; adb logcat -d | grep -i -E "python|crossposter|chaquopy|AndroidRuntime" | tail -80
exit 1
