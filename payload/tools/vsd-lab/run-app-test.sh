#!/bin/sh
# 验证「把应用放上虚拟屏并取到画面」：起新版守护 → am start 到副屏 → 取帧
DEXSRC=/root/vsd-test/vsd-new/bin/classes.dex
PORT=18274
A=$(cat /sdcard/dsh/.adb-serial)
DEXDEST="/data/local/tmp/vsd-test-$PORT.dex"

adb -s "$A" push "$DEXSRC" "$DEXDEST" >/dev/null 2>&1
adb -s "$A" shell "rm -rf /data/local/tmp/dalvik-cache; rm -f /data/local/tmp/vsd-test-$PORT.log; nohup sh -c 'CLASSPATH=$DEXDEST app_process /system/bin dsh.VsDaemon $PORT' > /data/local/tmp/vsd-test-$PORT.log 2>&1 & echo started" >/dev/null
sleep 10
echo "--- 建屏后 ---"
curl -s -m 5 "http://127.0.0.1:$PORT/list"; echo
DID=$(curl -s -m 5 "http://127.0.0.1:$PORT/list" | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['displayId'])")
echo "  第一块屏 displayId=$DID"
echo "--- 放「设置」到副屏 ---"
adb -s "$A" shell "am start --display $DID --windowingMode 5 -n com.android.settings/.Settings" 2>&1 | tr -d '\r' | head -5
sleep 5
echo "--- 放应用后 /list ---"
curl -s -m 5 "http://127.0.0.1:$PORT/list"; echo
echo "--- 取帧 ---"
curl -s -m 10 -o /root/vsd-test/app-frame.jpg -w "  屏0 取帧 http=%{http_code} 耗时=%{time_total}s 字节=%{size_download}\n" "http://127.0.0.1:$PORT/0/frame"
file /root/vsd-test/app-frame.jpg 2>/dev/null | sed 's/^/  /'
PID=$(adb -s "$A" shell "ps -A -o PID,ARGS | grep [V]sDaemon | grep $PORT | awk '{print \$1}'" | tr -d '\r')
for p in $PID; do adb -s "$A" shell "kill -9 $p" >/dev/null 2>&1; done
adb -s "$A" shell "rm -f $DEXDEST" >/dev/null 2>&1
echo "--- 已清理（屏与应用随之销毁） ---"
