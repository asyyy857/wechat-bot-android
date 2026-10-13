#!/bin/sh
# start.sh —— 以 shell 身份启动可信屏守护（受信任虚拟屏 + HTTP API）
#   用法: sh start.sh [端口=18240]        必须由 adb 拉起（shell uid）
. "$(dirname "$0")/../env.sh"
cd "$(dirname "$0")"
PORT="${1:-$VSD_PORT}"
[ -n "$SERIAL" ] || { echo "❌ 读不到 adb 串号（$DSH_ROOT/.adb-serial）"; exit 1; }
DEX=/data/local/tmp/vs-daemon.dex
adb -s "$SERIAL" push bin/classes.dex "$DEX" >/dev/null 2>&1 || { echo "❌ push 失败"; exit 1; }
# 按 PID 精确杀旧实例（pkill -f 会杀掉自己那条 shell）
OLD=$(adb -s "$SERIAL" shell "ps -A -o PID,ARGS | grep [V]sDaemon | awk '{print \$1}'" | tr -d '\r')
for p in $OLD; do adb -s "$SERIAL" shell "kill -9 $p" >/dev/null 2>&1; done
# ★坑：陈旧 dalvik-cache 会让 app_process 直接 Aborted/Killed
adb -s "$SERIAL" shell "rm -rf /data/local/tmp/dalvik-cache; rm -f /data/local/tmp/vs-daemon.log; nohup sh -c 'CLASSPATH=$DEX app_process /system/bin dsh.VsDaemon $PORT' > /data/local/tmp/vs-daemon.log 2>&1 & echo started" | tr -d '\r' | tail -1
sleep 9
adb -s "$SERIAL" shell "cat /data/local/tmp/vs-daemon.log" | tr -d '\r' | sed 's/^/  /'
