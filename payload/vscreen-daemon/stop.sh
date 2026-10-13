#!/bin/sh
# stop.sh —— 停掉可信屏守护（注意：会同时杀掉该屏上的所有应用）
. "$(dirname "$0")/../env.sh"
[ -n "$SERIAL" ] || { echo "❌ 读不到 adb 串号"; exit 1; }
OLD=$(adb -s "$SERIAL" shell "ps -A -o PID,ARGS | grep [V]sDaemon | awk '{print \$1}'" | tr -d '\r')
echo "kill: $OLD"
for p in $OLD; do adb -s "$SERIAL" shell "kill -9 $p" >/dev/null 2>&1; done
