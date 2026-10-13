#!/bin/sh
# adb-pair.sh —— 一次性配对 + 接上 adb（配对码来自手机「无线调试 → 使用配对码配对设备」）
#
#   用法: sh adb-pair.sh <配对端口|IP:端口> <6位配对码> [调试IP:端口]
#     例 : sh adb-pair.sh 41234 123456
#          → 配对后交给 adb-bootstrap.cjs 自己扫描调试端口（推荐，端口每次都会变）
#          sh adb-pair.sh 127.0.0.1:41234 123456 127.0.0.1:41753
#          → 两头都手动指定
#
#   做完自动把新密钥落盘到 /run/dsh-phone/adb-keys/（= App 私有目录的 bind mount，
#   容器重建不丢、别的应用读不到），以后容器重置就免重新配对。持久化逻辑在
#   adb-bootstrap.cjs 里。
#
#   来源：v1.1 文档附录「16. adb 一次性配对 · adb-pair.sh」。
#   2026-10-05 按本机情况改了两处：
#     ① 原版默认调试端口写死 `26.26.26.1:45119`（那是文档作者的设备），本机走回环；
#     ② 无线调试的**调试端口每次重开都会变** —— 所以不硬编码任何端口，
#        不给第三个参数就让 adb-bootstrap.cjs 去扫。
export HOME=/root PATH=/usr/bin:/opt/node-v22.23.2-linux-arm64/bin:$PATH

PAIR="$1"; CODE="$2"; DEBUG="$3"
[ -n "$PAIR" ] && [ -n "$CODE" ] || { echo "用法: sh $0 <配对端口|IP:端口> <6位配对码> [调试IP:端口]"; exit 2; }

# 只给端口号时补回环地址（容器与手机共享网络，实测 127.0.0.1 可用）
case "$PAIR" in
  *:*) ;;
  *)   PAIR="127.0.0.1:$PAIR" ;;
esac

echo "== 1) 先确认配对端口活着 =="
(exec 3<>/dev/tcp/${PAIR%:*}/${PAIR#*:}) 2>/dev/null && echo "  端口可用：$PAIR" \
  || { echo "  ❌ $PAIR 拒绝连接（配对弹窗关掉/超时了，请重新点开再配对）"; exit 3; }

echo "== 2) 配对 =="
timeout 60 adb pair "$PAIR" "$CODE" 2>&1

if [ -n "$DEBUG" ]; then
  echo "== 3) 连接调试端口 $DEBUG =="
  for i in 1 2 3; do
    timeout 40 adb connect "$DEBUG" 2>&1
    sleep 3
    if adb devices | grep -q "${DEBUG}[[:space:]]*device"; then echo "  ✅ 已连接"; break; fi
    echo "  第 $i 次未就绪，重试…"
  done
else
  echo "== 3) 未指定调试端口 → 交给 adb-bootstrap.cjs 扫描并连接 =="
fi
adb devices -l 2>&1

echo "== 4) 落盘密钥（容器重置后免配对） =="
node /sdcard/dsh/tools/adb-bootstrap.cjs
tail -4 /sdcard/dsh/logs/adb-bootstrap.log
echo "  持久化副本："; ls -la /run/dsh-phone/adb-keys/ 2>&1 | tail -5

SER=$(adb devices | awk '/device$/{print $1; exit}')
if [ -n "$SER" ]; then
  echo "$SER" > /sdcard/dsh/.adb-serial
  echo "  ✅ 已记下串号: $SER"
fi
