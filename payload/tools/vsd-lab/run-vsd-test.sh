#!/bin/sh
# 起一个临时端口的虚拟屏守护，查绑定地址 + 抓帧 + 抓物理屏做镜像比对，测完杀掉
# 用法: sh run-vsd-test.sh <dex路径> <端口> <标签>
DEXSRC="$1"; PORT="$2"; TAG="$3"
A=$(cat /sdcard/dsh/.adb-serial)
DEXDEST="/data/local/tmp/vsd-test-$PORT.dex"

adb -s "$A" push "$DEXSRC" "$DEXDEST" >/dev/null 2>&1 || { echo "push 失败"; exit 1; }
adb -s "$A" shell "rm -rf /data/local/tmp/dalvik-cache; rm -f /data/local/tmp/vsd-test-$PORT.log; nohup sh -c 'CLASSPATH=$DEXDEST app_process /system/bin dsh.VsDaemon $PORT' > /data/local/tmp/vsd-test-$PORT.log 2>&1 & echo started" >/dev/null
sleep 10
echo "--- $TAG 守护日志 ---"
adb -s "$A" shell "cat /data/local/tmp/vsd-test-$PORT.log" | tr -d '\r' | sed 's/^/  /'
echo "--- $TAG 端口绑定（安全相关） ---"
adb -s "$A" shell "ss -ltn 2>/dev/null | grep $PORT" | tr -d '\r' | sed 's/^/  /'
echo "--- $TAG 取帧 ---"
/opt/node-v22.23.2-linux-arm64/bin/node /root/vsd-test/frame-bench.cjs "$PORT" "[$TAG]"
echo "--- $TAG 物理屏截图（只用于像素比对，不看内容） ---"
adb -s "$A" exec-out screencap -p > "/root/vsd-test/phys-$PORT.png" 2>/dev/null
ls -l "/root/vsd-test/phys-$PORT.png" | sed 's/^/  /'
PID=$(adb -s "$A" shell "ps -A -o PID,ARGS | grep [V]sDaemon | grep $PORT | awk '{print \$1}'" | tr -d '\r')
for p in $PID; do adb -s "$A" shell "kill -9 $p" >/dev/null 2>&1; done
adb -s "$A" shell "rm -f $DEXDEST" >/dev/null 2>&1
echo "--- $TAG 已清理 ---"
