#!/bin/sh
# start.sh —— 启动命令端点守护（必须用 adb 起一次）
#   用法: sh start.sh [端口=18242]
. "$(dirname "$0")/../env.sh"
cd "$(dirname "$0")"
PORT="${1:-$SHD_PORT}"
[ -n "$SERIAL" ] || { echo "❌ 读不到 adb 串号（$DSH_ROOT/.adb-serial）"; exit 1; }
DEX=/data/local/tmp/shell-daemon.dex
adb -s "$SERIAL" push bin/classes.dex "$DEX" >/dev/null 2>&1 || { echo "❌ push 失败"; exit 1; }
OLD=$(adb -s "$SERIAL" shell "ps -A -o PID,ARGS | grep [S]hellDaemon | awk '{print \$1}'" | tr -d '\r')
for p in $OLD; do adb -s "$SERIAL" shell "kill -9 $p" >/dev/null 2>&1; done
adb -s "$SERIAL" shell "rm -rf /data/local/tmp/dalvik-cache; rm -f /data/local/tmp/shd.log; nohup sh -c 'CLASSPATH=$DEX app_process /system/bin dsh.ShellDaemon $PORT' > /data/local/tmp/shd.log 2>&1 & echo started" | tr -d '\r' | tail -1
sleep 4
adb -s "$SERIAL" shell "head -6 /data/local/tmp/shd.log" | tr -d '\r' | sed 's/^/  /'
