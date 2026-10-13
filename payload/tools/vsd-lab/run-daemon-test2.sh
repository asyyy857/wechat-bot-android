#!/bin/sh
# 临时端口起指令守护并跑专项测试，测完清理
DEXSRC="$1"; PORT="$2"; TAG="$3"
A=$(cat /sdcard/dsh/.adb-serial); TOK=$(cat /sdcard/dsh/.shd-token)
DEXDEST="/data/local/tmp/shd-test-$PORT.dex"
adb -s "$A" push "$DEXSRC" "$DEXDEST" >/dev/null 2>&1 || { echo "push 失败"; exit 1; }
adb -s "$A" shell "rm -rf /data/local/tmp/dalvik-cache; rm -f /data/local/tmp/shd-test-$PORT.log; nohup sh -c 'CLASSPATH=$DEXDEST app_process /system/bin dsh.ShellDaemon $PORT' > /data/local/tmp/shd-test-$PORT.log 2>&1 & echo started" >/dev/null
sleep 4
echo "--- $TAG 守护日志 ---"; adb -s "$A" shell "cat /data/local/tmp/shd-test-$PORT.log" | tr -d '\r' | sed 's/^/  /'
echo "--- $TAG /health ---"; curl -s -m 5 -H "X-Token: $TOK" "http://127.0.0.1:$PORT/health"; echo
echo "--- $TAG 实测 ---"; /opt/node-v22.23.2-linux-arm64/bin/node /root/vsd-test/exec-bench2.cjs "$PORT" "$TOK" "[$TAG]"
PID=$(adb -s "$A" shell "ps -A -o PID,ARGS | grep [S]hellDaemon | grep $PORT | awk '{print \$1}'" | tr -d '\r')
for p in $PID; do adb -s "$A" shell "kill -9 $p" >/dev/null 2>&1; done
adb -s "$A" shell "rm -f $DEXDEST /data/local/tmp/shd-test-$PORT.log" >/dev/null 2>&1
echo "--- $TAG 已清理 ---"
