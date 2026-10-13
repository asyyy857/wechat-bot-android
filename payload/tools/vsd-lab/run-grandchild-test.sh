#!/bin/sh
# 专项验证：超时后孙进程是否被整组清理干净
DEXSRC=/sdcard/dsh/shell-daemon/bin/classes.dex; PORT=18266
A=$(cat /sdcard/dsh/.adb-serial); TOK=$(cat /sdcard/dsh/.shd-token)
DEXDEST="/data/local/tmp/shd-test-$PORT.dex"
adb -s "$A" push "$DEXSRC" "$DEXDEST" >/dev/null 2>&1
adb -s "$A" shell "rm -rf /data/local/tmp/dalvik-cache; nohup sh -c 'CLASSPATH=$DEXDEST app_process /system/bin dsh.ShellDaemon $PORT' > /data/local/tmp/shd-test-$PORT.log 2>&1 & echo started" >/dev/null
sleep 4
cnt() { adb -s "$A" shell 'ps -A -o ARGS | grep "sleep 300" | grep -c sleep || true' | tr -d '\r'; }
echo "  测试前，设备上 sleep 300 数量 = $(cnt)"
curl -s -m 30 -H "X-Token: $TOK" --data 'sleep 300 & sleep 300' "http://127.0.0.1:$PORT/exec" > /root/vsd-test/grandchild-out.txt &
CP=$!
sleep 3
echo "  命令启动 3s 后（应为 2~3 个） = $(cnt)"
wait $CP
echo "  超时响应 = $(cat /root/vsd-test/grandchild-out.txt | tr -d '\n')"
sleep 2
echo "  超时清理后（应为 0） = $(cnt)"
PID=$(adb -s "$A" shell "ps -A -o PID,ARGS | grep [S]hellDaemon | grep $PORT | awk '{print \$1}'" | tr -d '\r')
for p in $PID; do adb -s "$A" shell "kill -9 $p" >/dev/null 2>&1; done
adb -s "$A" shell "rm -f $DEXDEST /data/local/tmp/shd-test-$PORT.log" >/dev/null 2>&1
echo "  已清理测试守护"
